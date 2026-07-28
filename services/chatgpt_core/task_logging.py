"""Safe logging helpers for ChatGPT task logs.

Runtime code may still need raw proxy/API/token/password/OTP values.  This module
only prepares display/log/history copies and must stay dependency-free.
"""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

REDACTION_VERSION = "chatgpt-task-logging-redaction-v1"
REDACTED = "[REDACTED]"
REDACTED_TOKEN = "[REDACTED_TOKEN]"
REDACTED_OTP = "[REDACTED_OTP]"
REDACTED_URL = "[REDACTED_URL]"

_URL_RE = re.compile(r"https?://[^\s'\"<>｜|，,；;]+", re.I)
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


def format_http_trace_log(
    method: Any,
    url: Any,
    *,
    status: Any = "",
    duration_ms: Any = "",
    page: Any = "",
    resource_type: Any = "",
    request_bytes: Any = "",
    response_bytes: Any = "",
    error: Any = "",
) -> str:
    """Build a display-safe registration network transaction line.

    Only the request method, host/path, status, timing and coarse sizes are
    retained. Query strings, fragments, headers and bodies are intentionally
    excluded because they commonly contain OTPs, cookies or bearer tokens.
    """

    raw_url = str(url or "").strip()
    try:
        # ``urlsplit`` treats a scheme-less endpoint as a path.  Parse it with
        # a temporary scheme so query/fragment stripping also applies to the
        # compact host/path form emitted by some transports.  Rebuild the
        # authority from hostname/port instead of ``netloc`` so userinfo can
        # never enter a network trace.
        if raw_url.startswith("/") and not raw_url.startswith("//"):
            endpoint = raw_url.split("?", 1)[0].split("#", 1)[0] or "/"
        else:
            parse_target = raw_url if "://" in raw_url else f"https://{raw_url.lstrip('/')}"
            parts = urlsplit(parse_target)
            if parts.netloc:
                hostname = str(parts.hostname or "").strip()
                try:
                    port = f":{parts.port}" if parts.port else ""
                except ValueError:
                    port = ""
                if ":" in hostname and not hostname.startswith("["):
                    hostname = f"[{hostname}]"
                endpoint = f"{hostname}{port}{parts.path or '/'}"
            else:
                endpoint = redact_url(raw_url.split("?", 1)[0].split("#", 1)[0])
    except Exception:
        endpoint = redact_url(raw_url.split("?", 1)[0].split("#", 1)[0])
    endpoint = _truncate(endpoint or "-", 220)
    verb = str(method or "GET").strip().upper() or "GET"
    status_text = str(status or "-").strip() or "-"
    try:
        elapsed = f"{float(duration_ms):g}ms" if duration_ms not in (None, "") else "-"
    except (TypeError, ValueError):
        elapsed = f"{str(duration_ms).strip()}ms" if str(duration_ms).strip() else "-"
    chunks = [f"[HTTP] {verb} {endpoint} -> {status_text} {elapsed}"]
    page_text = str(page or "").strip()
    if page_text:
        chunks.append(f"page={redact_log_text(page_text)}")
    resource_text = str(resource_type or "").strip()
    if resource_text:
        chunks.append(f"type={redact_log_text(resource_text)}")
    for label, value in (("req_bytes", request_bytes), ("resp_bytes", response_bytes)):
        if value in (None, ""):
            continue
        try:
            chunks.append(f"{label}={max(int(value), 0)}")
        except (TypeError, ValueError):
            continue
    error_text = redact_log_text(error).strip()
    # Network traces are intentionally identity-free; keep an error's reason
    # while removing even the masked mailbox address that the generic redactor
    # would otherwise retain for operator context.
    error_text = re.sub(
        r"(?i)(?:[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+)@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        "[EMAIL]",
        error_text,
    )
    error_text = re.sub(r"(?i)\bemail(?:_addr|_address)?\s*[:=]\s*[^\s｜|,，;；]+", "email=[EMAIL]", error_text)
    if error_text:
        chunks.append(f"error={_truncate(error_text, 180)}")
    return " ".join(chunks)


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


def mask_email_for_log(value: Any) -> str:
    """Compact an email for concurrent task context without losing identity."""

    text = str(value or "").strip()
    if not text or "@" not in text:
        return text[:24]
    local, domain = text.rsplit("@", 1)
    if len(local) <= 3:
        masked_local = f"{local[:1]}***"
    else:
        masked_local = f"{local[:3]}***{local[-1:]}"
    return f"{masked_local}@{domain}"


def redact_raw_phone_line(value: Any) -> str:
    """Redact a pasted ``phone----api_url`` line for task display/history."""

    text = str(value or "").strip()
    if not text:
        return ""
    parts = re.split(r"-{3,}", text, maxsplit=1)
    if len(parts) == 2:
        phone, api_url = parts
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
    # Longer authorization-code-like values.  Do not redact endpoint names such
    # as ``phone-otp/send`` when they appear after a Chinese "验证码:" context in
    # an error sentence; those are route diagnostics, not secrets.
    def repl_long(match: re.Match[str]) -> str:
        candidate = str(match.group(3) or "")
        if "/" in candidate or "\\" in candidate:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}{REDACTED_OTP}"

    text = re.sub(
        rf"({contexts})(\s*[:：=]\s*)([A-Za-z0-9._~+/=-]{{8,}})(?:\.\.\.)?",
        repl_long,
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
        r"(?i)(密码|登录密码|账号密码)(\s*[:：=]\s*)[^\s,，;；}\]\)]+",
        rf"\1\2{REDACTED}",
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
        r"(?<!\S)(\+?\d[\d().\s-]{6,}\d)-{3,}(https?://[^\s'\"<>]+)",
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


_COMMON_INFO_PREFIXES = (
    "[SUMMARY]",
    "[OK]",
    "[FAIL]",
    "[SKIP]",
    "[STOP]",
    "[ERROR]",
    "[WARN]",
    "[MISS]",
    "[控制]",
    "[代理]",
    "[结果]",
)

_REGISTER_INFO_PREFIXES = (
    "create_account:",
    "创建失败:",
    "client_auth_session_dump",
    "请求模式:",
    "有效运输层:",
    "Camoufox 注册链路",
    "[阶段]",
    "[路由]",
    "[已有账号]",
    "[账号]",
    "[主链路]",
    "[注册]",
    "[登录]",
    "[邮箱]",
    "[验证码]",
    "[邀请]",
    "[business]",
    "[free]",
    "[K12]",
    "[Workspace]",
    "[TempMailLocal]",
    "[iCloudHME]",
    "[Auto Upload]",
    "[SKIP_SAVE]",
    "[升级链接]",
    "[GoPay]",
    "[OaiPay]",
)

_PHONE_SIGNUP_INFO_PREFIXES = (
    "[手机号注册]",
    "[手机号注册号段]",
    "[接码网关]",
    "[号码池]",
)

_PHONE_BINDING_INFO_PREFIXES = (
    "[手机号绑定]",
    "[手机号池]",
    "[限定号段]",
    "[号段抽样]",
)

_LOW_LEVEL_DEBUG_PREFIXES = (
    "开始 OAuth 登录流程",
    "OAuth 策略",
    "OAuth 状态起点",
    "OAuth 指纹",
    "注册状态机参数",
    "注册状态起点",
    "注册状态推进",
    "状态步进[",
    "follow[",
    "follow ->",
    "follow state ->",
    "workspace 解析入口",
    "workspace 候选",
    "workspace session 数据为空",
    "consent 页面请求 ->",
    "Sentinel Browser",
    "Sentinel:",
    "browser bootstrap",
    "force_new_browser",
    "Authorize →",
    "authorize ->",
    "authorize redirects ->",
    "authorize_continue",
    "访问 ChatGPT 首页",
    "获取 CSRF token",
    "CSRF token",
    "提交邮箱:",
    "获取到 authorize URL",
    "访问 authorize URL",
    "重定向到:",
    "验证码发送状态:",
    "验证码发送响应:",
    "验证 OTP 码:",
    "验证成功 ",
    "Session Account ID:",
    "Session User ID:",
    "Session Workspace ID:",
    "Account ID:",
    "Workspace ID:",
    "实现策略:",
    "流程策略:",
    "验证码等待策略:",
    "邮箱:",
    "密码:",
    "注册信息:",
    "正在创建 ",
    "生成固定域名邮箱:",
    "命中验证码:",
    "成功获取验证码（",
    "正在等待邮箱 ",
    "复用已登录 auth 会话抓取 workspace",
    "复用会话状态步进[",
    "复用会话遇到未支持的 OAuth 状态",
    "Plus 账单探测:",
    "Plus checkout created:",
    "Plus checkout amount:",
    "GoPay 平台链接:",
    "GoPay 平台链接已获取:",
    "password/verify ->",
    "user/register ->",
    "phone-otp/send ->",
    "resend ->",
    "validate ->",
    "callback ->",
    "/me ->",
    "home ->",
    "providers ->",
    "csrf ->",
    "params ->",
    "authorize_url ->",
)

_LOW_LEVEL_DEBUG_CONTAINS = (
    "page=",
    "method=GET next=",
    "method=POST next=",
    "workspace/select ->",
    "organization/select ->",
    "email_otp_validate:",
    "passwordless OTP 已触发",
    "OAuth OTP 等待窗口:",
    "使用 wait_for_verification_code 进行阻塞式获取新验证码",
    "/oauth/authorize ->",
    "/authorize/continue ->",
    "/passwordless/send-otp ->",
    "/email-otp/validate ->",
    "/email-otp/send ->",
    "/phone-otp/",
    "login_session: 已获取",
    "authorize_continue 分支判定:",
    "等待 OTP 异常:",
    "已触发 email-otp 重发",
    "暂未收到新的 OTP",
    "尝试 OTP:",
    "session 中没有 workspace 信息",
    "oai-client-auth-session 已存在",
    "从 oai-client-auth-session cookie 读取到",
    "选择 workspace:",
    "选择 organization:",
    "response=",
    "响应:",
    "HTTP ",
    "status=",
    "route=",
    "redirects=",
)


def _strip_leading_bracket_tags(text: str) -> str:
    normalized = str(text or "").strip()
    while normalized.startswith("[") and "]" in normalized:
        _, _, rest = normalized.partition("]")
        if not rest:
            break
        normalized = rest.strip()
    return normalized


_REGISTER_FORCE_INFO_MARKERS = (
    "registration_disallowed",
    "create_account:",
    "创建失败:",
    "client_auth_session_dump",
    "create_account Sentinel:",
    "密码阶段 Sentinel:",
    "协议模式：提交 authorize/continue",
    "signup continue",
    "sentinel_protocol_unavailable",
    "sentinel_browser_unavailable",
    "auth_browser_finalize_unavailable",
    "有效运输层",
    "effective_transport",
    "effective_executor",
    "registration_transport",
    "Camoufox 注册链路",
    "HTTP 400",
    "创建账号失败",
)


def classify_task_log_level(
    message: Any,
    level: str = "info",
    *,
    flow: str = "",
) -> str:
    """Classify task logs into the UI-facing ``info`` or ``debug`` streams.

    ``warning`` and ``error`` intentionally remain non-debug so operators see
    failures in the default Info tab.  Low-level HTTP/OAuth/Sentinel/state-machine
    chatter goes to Debug unless the caller explicitly marks it as a business
    stage line.
    """

    normalized_level = str(level or "info").strip().lower() or "info"
    if normalized_level == "warn":
        normalized_level = "warning"
    if normalized_level in {"debug", "warning", "error"}:
        return normalized_level

    text = str(message or "").strip()
    if not text:
        return "info"
    upper = text.lstrip().upper()
    if upper.startswith("[DEBUG]"):
        return "debug"
    # 方案 R：create 400 / disallowed / dump / transport 对任务 UI 必须可见
    lowered = text.lower()
    if lowered.startswith("create_account:") and "sentinel token" in lowered:
        return "debug"
    if any(marker.lower() in lowered for marker in _REGISTER_FORCE_INFO_MARKERS):
        return "info"
    if text.startswith("="):
        return "debug"
    if text[:2].isdigit() and len(text) > 2 and text[2] == ".":
        return "debug"

    flow_key = str(flow or "").strip().lower().replace("-", "_")
    info_prefixes = list(_COMMON_INFO_PREFIXES)
    if flow_key in {"register", "rt_register", "refresh_token_register", "access_token_register", "resume_auth", "k12"}:
        info_prefixes.extend(_REGISTER_INFO_PREFIXES)
    if flow_key in {"phone_signup", "phone_registration"}:
        info_prefixes.extend(_PHONE_SIGNUP_INFO_PREFIXES)
    if flow_key in {"phone_binding", "phone_binding_test"}:
        info_prefixes.extend(_PHONE_BINDING_INFO_PREFIXES)

    if text.startswith(tuple(info_prefixes)):
        return "info"
    if text.startswith("[") and "]" in text:
        return "debug"

    normalized_text = _strip_leading_bracket_tags(text)
    if normalized_text.startswith(_LOW_LEVEL_DEBUG_PREFIXES) or text.startswith(_LOW_LEVEL_DEBUG_PREFIXES):
        return "debug"
    if any(marker in text for marker in _LOW_LEVEL_DEBUG_CONTAINS):
        return "debug"
    return "info"


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


def _clean_phone_binding_body(value: Any) -> str:
    text = str(value or "").strip()
    while True:
        cleaned = _PHONE_BINDING_MODULE_PREFIX_RE.sub("", text)
        if cleaned == text:
            return text
        text = cleaned.strip()


_PHONE_BINDING_FIELD_LABELS = {
    "account_id": "账号ID",
    "accountid": "账号ID",
    "账号id": "账号ID",
    "手机号序号": "号码序号",
    "source": "来源",
    "country": "目标国家",
    "actual": "实际国家",
    "actual_country": "实际国家",
    "actualcountry": "实际国家",
    "exit_ip": "出口IP",
    "exitip": "出口IP",
    "provider": "供应商",
    "sid": "SID",
    "probe": "探测",
    "proxy": "代理",
    "timeout": "超时",
    "otp": "验证码",
    "otp_received": "收码",
    "otpreceived": "收码",
    "otp_length": "长度",
    "otplength": "长度",
}


def _phone_binding_field_label(key: Any) -> str:
    raw = str(key or "").strip()
    compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", raw.lower())
    return _PHONE_BINDING_FIELD_LABELS.get(raw) or _PHONE_BINDING_FIELD_LABELS.get(compact) or raw


def _phone_binding_field_value(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered == "true":
        return "是"
    if lowered == "false":
        return "否"
    if lowered == "ok":
        return "正常"
    return text


def _join_phone_binding_fields(items: list[tuple[str, Any]]) -> str:
    chunks: list[str] = []
    for key, value in items:
        label = _phone_binding_field_label(key)
        safe_value = _phone_binding_field_value(value)
        if not label or safe_value == "":
            continue
        chunks.append(f"{label}={safe_value}")
    return "｜".join(chunks)


def _phone_binding_search(pattern: str, text: str, *, flags: int = re.I) -> str:
    match = re.search(pattern, text, flags)
    return str(match.group(1) or "").strip() if match else ""


def _normalize_phone_binding_key_values(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""

    normalized = normalized.replace("|", "｜")
    normalized = re.sub(r"\s+(?=(?:country|actual|exit_ip|provider|sid|probe|source|proxy|timeout|otp|otp_received|otp_length)\s*[:：=])", "｜", normalized, flags=re.I)
    normalized = re.sub(r"\s*[，,；;]\s*", "｜", normalized)
    chunks: list[str] = []
    for raw_part in normalized.split("｜"):
        part = raw_part.strip()
        if not part:
            continue
        match = re.match(r"^([^:：=\s]{1,24})\s*[:：=]\s*(.+)$", part)
        if not match:
            chunks.append(part)
            continue
        key = _phone_binding_field_label(match.group(1))
        value = _phone_binding_field_value(match.group(2))
        chunks.append(f"{key}={value}")
    return "｜".join(chunks)


def _normalize_phone_binding_detail(value: Any) -> str:
    text = _clean_phone_binding_body(value)
    if not text:
        return ""
    text = re.sub(r"^使用账号[:：]\s*([^，,｜|]+)", r"邮箱：\1", text)

    if text.startswith("使用代理") or "使用代理" in text:
        fields = [
            ("序号", _phone_binding_search(r"使用代理\s*([0-9]+/[0-9]+)", text)),
            ("来源", _phone_binding_search(r"(?:来源|source)\s*[:：=]\s*([^｜\s,，;；]+)", text)),
            ("目标国家", _phone_binding_search(r"\bcountry\s*[:：=]\s*([^｜\s,，;；]+)", text)),
            ("实际国家", _phone_binding_search(r"\bactual\s*[:：=]\s*([^｜\s,，;；]+)", text)),
            ("出口IP", _phone_binding_search(r"\bexit_ip\s*[:：=]\s*([^｜\s,，;；]+)", text)),
            ("供应商", _phone_binding_search(r"\bprovider\s*[:：=]\s*([^｜\s,，;；]+)", text)),
            ("SID", _phone_binding_search(r"\bsid\s*[:：=]\s*([^｜\s,，;；]+)", text)),
            ("探测", _phone_binding_search(r"\bprobe\s*[:：=]\s*([^｜\s,，;；]+)", text)),
            ("代理", _phone_binding_search(r"(?:代理|proxy)\s*[:：=]\s*([^｜\s,，;；]+)", text)),
        ]
        return _join_phone_binding_fields(fields) or _normalize_phone_binding_key_values(text)

    proxy_attempt = _phone_binding_search(r"代理\s*([0-9]+/[0-9]+)", text)
    if text.startswith("开始 OAuth 登录") and proxy_attempt:
        return _join_phone_binding_fields([("代理序号", proxy_attempt)])

    if text.startswith("等待邮箱验证码"):
        phase = _phone_binding_search(r"等待邮箱验证码\s*[:：]\s*([^｜]+)", text)
        timeout = _phone_binding_search(r"(?:超时|timeout)\s*[:：=]\s*([^｜\s]+)", text)
        return _join_phone_binding_fields([("验证码类型", phase), ("超时", timeout)])

    if text.startswith("验证码已获取"):
        phase = _phone_binding_search(r"验证码已获取\s*[:：]\s*(.+)$", text)
        return _join_phone_binding_fields([("验证码类型", phase)])

    if text.startswith("OpenAI 已接受手机号"):
        return _join_phone_binding_fields([("OpenAI", "已接受手机号"), ("下一步", "等待短信验证码")])

    if "收到验证码" in text:
        phone = _phone_binding_search(r"(\+\d[\d*]+)\s*收到验证码", text)
        otp = REDACTED_OTP if REDACTED_OTP in text else _phone_binding_search(r"\botp\s*[:：=]\s*([^｜\s,，;；]+)", text)
        received = _phone_binding_search(r"\botp_received\s*[:：=]\s*([^｜\s,，;；]+)", text)
        length = _phone_binding_search(r"\botp_length\s*[:：=]\s*([^｜\s,，;；]+)", text)
        code_time = _phone_binding_search(r"时间\s*([^，,｜]+)", text)
        handling = "已提取6位" if "已提取" in text else ""
        return _join_phone_binding_fields([
            ("手机号", phone),
            ("验证码", otp),
            ("收码", received),
            ("长度", length),
            ("短信时间", code_time),
            ("处理", handling),
        ])

    if "手机号验证码" in text and ("通过" in text or "绑定完成" in text):
        return _join_phone_binding_fields([("验证码", "已通过"), ("OpenAI", "已绑定")])

    if "Auth/RT 获取成功" in text:
        return _join_phone_binding_fields([("Auth", "已获取"), ("RT", "已获取")])

    if text.startswith("已写入账号绑定状态"):
        field = _phone_binding_search(r"已写入账号绑定状态\s+(.+)$", text)
        return _join_phone_binding_fields([("字段", field)])

    if text.startswith("已回写号码池"):
        result = _phone_binding_search(r"已回写号码池\s*[:：]\s*(.+)$", text)
        return _join_phone_binding_fields([("结果", result)])

    return _normalize_phone_binding_key_values(text).strip(" ｜|，,；;")


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
    if "验证码已获取" in text or "已获取" in text:
        return "已获取"
    if "收到验证码" in text or "已收到" in text:
        return "已收到"
    if "已发码" in text or "已接受手机号" in text or "已接受并发送验证码" in text or "已请求绑定手机号短信验证码" in text:
        return "已发码"
    if "等待" in text:
        return "等待"
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
    if detail:
        return f"{stage_tag} {status}｜{detail}"
    return f"{stage_tag} {status}".rstrip()


_REGISTRATION_MODULE_PREFIX_RE = re.compile(
    r"^\s*\[(?:控制|代理|账号|指纹|邮箱|验证码|保存|iCloudHME|TempMailLocal|阶段|路由|注册|登录|已有账号|SKIP_SAVE|升级链接|Auto Upload|Upload Gate|结果|DEBUG|OK|SKIP|FAIL|FATAL|STOP|WARN|ERROR)\]\s*",
    re.I,
)
_REGISTRATION_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/?^_`{|}~\-])"
    r"[A-Za-z0-9.!#$%&'*+/?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
_REGISTRATION_STAGE_LABELS: dict[int, str] = {
    1: "准备",
    2: "选择代理",
    3: "领取邮箱",
    4: "提交注册",
    5: "邮箱验证",
    6: "完成资料",
    7: "建立会话",
    8: "保存与同步",
    9: "完成",
}
_REGISTRATION_FIELD_LABELS = {
    "email": "邮箱",
    "email_address": "邮箱",
    "emailaddress": "邮箱",
    "target": "目标",
    "current_success": "已成功",
    "success_slot": "成功位",
    "registration_success_slot": "成功位",
    "executor": "执行器",
    "concurrency": "并发",
    "existing_account_action": "已有账号",
    "uncertain_browser_failure_slot": "不确定浏览器失败",
    "device": "设备",
    "chrome": "Chrome",
    "viewport": "视口",
    "lang": "语言",
    "sig": "指纹",
    "candidate": "候选",
    "source": "来源",
    "country": "目标国家",
    "actual": "实际国家",
    "provider": "供应商",
    "sid": "SID",
    "retention": "IP保留",
    "probe": "探测",
    "proxy": "代理",
    "exit_ip": "出口IP",
    "browser_identity_started": "浏览器身份",
    "same_attempt_proxy_failover": "同尝试切换代理",
    "mail_provider": "邮箱渠道",
    "provider_name": "邮箱渠道",
    "lease": "租约",
    "lease_id": "租约",
    "mailbox_id": "邮箱ID",
    "forward_mailbox_id": "转发箱ID",
    "mailbox_action": "邮箱动作",
    "timeout": "超时",
    "wait_seconds": "等待",
    "wait": "等待",
    "otp_wait_seconds": "等待",
    "otp_source": "来源",
    "otp_resend_count": "重发次数",
    "resend_count": "重发次数",
    "code_length": "长度",
    "otp_length": "长度",
    "next_page": "下一页",
    "page": "页面",
    "page_type": "页面",
    "duration_ms": "耗时",
    "elapsed_ms": "耗时",
    "request_bytes": "请求字节",
    "response_bytes": "响应字节",
    "content_type": "类型",
    "http_status": "HTTP",
    "status_code": "HTTP",
    "method": "方法",
    "path": "路径",
    "transport": "运输层",
    "account_id": "OpenAI账号",
    "inventory_id": "库存账号",
    "access_token": "AccessToken",
    "session_token": "SessionToken",
    "cookies": "Cookie",
    "at": "AT",
    "session": "Session",
    "cookie": "Cookie",
    "http": "HTTP",
    "stage": "阶段",
    "result": "结果",
    "saved": "已保存",
    "code": "原因码",
    "reason": "原因",
    "mailbox": "邮箱回写",
    "slot": "占用目标",
    "backfill": "补位",
    "certainty": "确定性",
    "progress": "进度",
}
_REGISTRATION_VALUE_LABELS = {
    "headless": "无头浏览器",
    "headed": "有头浏览器",
    "protocol": "协议",
    "specified": "指定代理",
    "dynamic": "动态代理",
    "pool": "代理池",
    "direct": "直连",
    "login_recovery": "登录恢复",
    "skip": "跳过",
    "consume": "占用",
    "started": "开始",
    "success": "成功",
    "failed": "失败",
    "skipped": "跳过",
    "stopped": "停止",
    "pending": "待补抓",
    "finalized": "已完成",
    "keep": "保留",
    "known": "已知",
    "unknown": "未知",
    "deterministic": "确定",
    "enabled": "启用",
    "disabled": "禁用",
    "refreshed": "已刷新",
    "unchanged": "未变化",
    "unverified": "未验证",
    "any_auto_browser": "any-auto 浏览器",
    "any_auto_protocol": "any-auto 协议",
    "hme_ready_api": "HME Helper API",
    "ok": "正常",
    "created": "已创建",
    "claimed_helper": "Helper已领取",
    "reused_existing": "复用已有",
    "registered_email": "注册邮箱",
}


def _clean_registration_body(value: Any) -> str:
    text = str(value or "").strip()
    while True:
        cleaned = _REGISTRATION_MODULE_PREFIX_RE.sub("", text)
        if cleaned == text:
            break
        text = cleaned.strip()
    return _REGISTRATION_EMAIL_RE.sub(
        lambda match: mask_email_for_log(match.group(0)),
        text,
    )


def _registration_field_label(key: Any) -> str:
    raw = str(key or "").strip()
    lowered = raw.lower().replace("-", "_")
    return _REGISTRATION_FIELD_LABELS.get(lowered, _REGISTRATION_FIELD_LABELS.get(raw.lower(), raw))


def _registration_field_value(key: Any, value: Any) -> str:
    raw_key = str(key or "").strip().lower().replace("-", "_")
    text = str(value or "").strip().strip("()")
    lowered = text.lower()
    if raw_key in {
        "code",
        "otp",
        "verification_code",
        "verificationcode",
        "email_otp",
        "emailotp",
        "phone_otp",
        "phoneotp",
        "authorization_code",
        "authorizationcode",
        "auth_code",
        "authcode",
    }:
        # The outer task logger redacts these too, but the timeline formatter is
        # also used directly by tests and standalone callers.  Never let a
        # numeric OTP reappear while normalizing ``code=...`` fields.
        if re.fullmatch(r"\d{4,8}", text):
            return REDACTED_OTP
        return text
    if raw_key in {"slot", "backfill", "browser_identity_started"}:
        if lowered in {"1", "yes", "true", "on"}:
            return "是"
        if lowered in {"0", "no", "false", "off"}:
            return "否"
    if raw_key == "same_attempt_proxy_failover":
        if lowered in {"disabled", "no", "false", "off"}:
            return "禁用"
        if lowered in {"enabled", "yes", "true", "on"}:
            return "启用"
    if raw_key == "retention":
        retention_match = re.fullmatch(r"t-(\d+)", lowered)
        if retention_match:
            return f"{retention_match.group(1)}分钟"
    if raw_key == "mailbox" and lowered == "success":
        return "已提交"
    if lowered in {"yes", "true", "on"}:
        return "是"
    if lowered in {"no", "false", "off"}:
        return "否"
    if raw_key == "timeout" and lowered.endswith("s") and lowered[:-1].isdigit():
        return f"{lowered[:-1]}秒"
    if raw_key in {"wait_seconds", "wait", "otp_wait_seconds"} and lowered.endswith("s") and lowered[:-1].isdigit():
        return f"{lowered[:-1]}秒"
    if raw_key in {"duration_ms", "elapsed_ms"} and lowered.endswith("ms"):
        return text
    return _REGISTRATION_VALUE_LABELS.get(lowered, text)


def _normalize_registration_key_values(value: Any) -> str:
    text = str(value or "").strip().replace("|", "｜")
    if not text:
        return ""
    text = re.sub(r"\(出口IP\s*[:：]\s*([^\)]+)\)", r"exit_ip=\1", text, flags=re.I)
    known_keys = "|".join(re.escape(key) for key in _REGISTRATION_FIELD_LABELS)
    text = re.sub(
        rf"\s+(?=(?:{known_keys})\s*[:：=])",
        "｜",
        text,
        flags=re.I,
    )
    chunks: list[str] = []
    for raw_part in text.split("｜"):
        part = raw_part.strip()
        if not part:
            continue
        match = re.match(r"^([^:：=\s]{1,40})\s*[:：=]\s*(.+)$", part)
        if not match:
            chunks.append(part)
            continue
        key = str(match.group(1) or "").strip()
        field_value = str(match.group(2) or "").strip()
        chunks.append(
            f"{_registration_field_label(key)}={_registration_field_value(key, field_value)}"
        )
    return "｜".join(chunks)


def infer_registration_timeline_stage(message: Any) -> tuple[int, str]:
    """Map one registration event to the stable nine-step operator timeline."""

    raw_lowered = str(message or "").lower()
    text = _clean_registration_body(message)
    lowered = text.lower()
    if any(marker in lowered for marker in ("outcome=", "完成: 成功", "失败: 成功", "任务已停止:")):
        index = 9
    elif any(
        marker in lowered
        for marker in (
            "auto upload",
            "upload gate",
            "自动同步外部系统",
            "跳过上传",
            "refresh_token",
            "helper 已提交",
            "外部同步",
            "plus 额度验证",
            "账号保存",
            "账号已保存",
            "auth_capture",
            "不保存账号",
            "升级链接",
            "已有账号",
        )
    ) or any(marker in raw_lowered for marker in ("[已有账号]", "[skip_save]", "[升级链接]")):
        index = 8
    elif "注册运输层" in lowered and "成功" not in lowered:
        index = 4
    elif any(
        marker in lowered
        for marker in (
            "web session",
            "api/auth/session",
            "accesstoken",
            "sessiontoken",
            "token 提取",
            "注册运输层成功",
            "oauth recovery",
            "注册流程成功结束",
        )
    ):
        index = 7
    elif any(
        marker in lowered
        for marker in (
            "验证码",
            "email_otp",
            "email-verification",
            "email-otp",
            "otp",
        )
    ):
        index = 5
    elif any(
        marker in lowered
        for marker in (
            "about_you",
            "about-you",
            "资料已提交",
            "账号创建完成",
        )
    ):
        index = 6
    elif any(
        marker in lowered
        for marker in (
            "注册运输层",
            "camoufox 整段",
            "邮箱入口已提交",
            "密码已提交",
            "提交注册",
            "api/accounts/user/register",
            "create-account/password",
        )
    ):
        index = 4
    elif any(
        marker in lowered
        for marker in (
            "mail_provider",
            "helper ready api 出池",
            "helper 已领取别名",
            "复用远端邮箱",
            "邮箱领取",
            "邮箱已获取",
            "转发箱",
        )
    ):
        index = 3
    elif any(
        marker in lowered
        for marker in (
            "代理",
            "proxy",
            "candidate=",
            "预检通过",
            "browser_identity_started",
            "出口 ip",
            "候选",
        )
    ):
        index = 2
    else:
        index = 1
    return index, _REGISTRATION_STAGE_LABELS[index]


def _registration_status_and_detail(message: Any) -> tuple[str, str]:
    text = _clean_registration_body(message)
    lowered = text.lower()
    explicit_status = re.search(r"(?:^|\s)status=([^\s｜,，;；]+)", text, flags=re.I)
    explicit_outcome = re.search(r"(?:^|\s)outcome=([^\s｜,，;；]+)", text, flags=re.I)
    explicit_result = re.search(r"(?:^|\s)result=([^\s｜,，;；]+)", text, flags=re.I)

    summary_match = re.match(
        r"^(完成|失败|任务已停止)\s*[:：]\s*成功\s*(\d+)\s*个\s*[,，]\s*跳过\s*(\d+)\s*个\s*[,，]\s*失败\s*(\d+)\s*个",
        text,
    )
    if summary_match:
        status = "成功" if summary_match.group(1) == "完成" else "失败" if summary_match.group(1) == "失败" else "已停止"
    elif explicit_outcome:
        status = _registration_field_value("outcome", explicit_outcome.group(1))
    elif "不保存账号" in text:
        status = "不保存"
    elif "升级链接" in text or text.startswith("http://") or text.startswith("https://"):
        status = "已生成"
    elif "致命" in text or "基础设施不可用" in text:
        status = "失败"
    elif "写入任务快照失败" in text or "写回失败" in text:
        status = "警告"
    elif explicit_result and str(explicit_result.group(1) or "").strip().lower() == "pending":
        status = "待补抓"
    elif "已达到注册最大尝试次数" in text:
        status = "停止补位"
    elif "any-auto 已返回 AccessToken/Session" in text:
        status = "已获取"
    elif "验证码已提交" in text and ("HTTP=200" in text or "http=200" in lowered):
        status = "提交成功"
    elif "已提交" in text and ("HTTP=200" in text or "http=200" in lowered):
        status = "提交成功"
    elif "已提交" in text:
        status = "已提交"
    elif "跳过上传" in text or "跳过" in text:
        status = "跳过"
    elif explicit_status:
        status = _registration_field_value("status", explicit_status.group(1))
    elif "命中验证码" in text or "收到验证码" in text or "验证码已收到" in text:
        status = "已收到"
    elif "验证码已获取" in text or "token 提取完成" in lowered:
        status = "已获取"
    elif "邮箱已获取" in text:
        status = "已获取"
    elif "账号已保存" in text:
        status = "已保存"
    elif "等待" in text:
        status = "等待"
    elif "预检通过" in text:
        status = "预检通过"
    elif "已分配" in text:
        status = "已分配"
    elif "已领取" in text:
        status = "已领取"
    elif "已选择" in text or "candidate=" in lowered:
        status = "已选择"
    elif (
        "配置" in text
        or "registration_policy" in lowered
        or "请求模式" in text
        or "注册最大尝试次数" in text
        or "mail_provider=" in lowered
    ):
        status = "配置"
    elif "browser_identity_started=" in lowered:
        status = "策略"
    elif "注册核心链路" in text:
        status = "已应用"
    elif "复用远端邮箱" in text:
        status = "已连接"
    elif "失败" in text or "[fail]" in lowered or "error" in lowered:
        status = "失败"
    elif "成功" in text or "完成" in text:
        status = "成功"
    elif "开始" in text or "启动" in text or "出池" in text or "执行 any-auto" in text:
        status = "开始"
    else:
        status = "信息"

    detail = text
    detail = re.sub(r"(?:^|\s)status=[^\s｜,，;；]+", "", detail, flags=re.I)
    detail = re.sub(r"(?:^|\s)outcome=[^\s｜,，;；]+", "", detail, flags=re.I)
    if summary_match:
        detail = (
            f"成功={summary_match.group(2)}｜"
            f"跳过={summary_match.group(3)}｜失败={summary_match.group(4)}"
        )
        summary_tail = text[summary_match.end() :].strip(" ;；")
        if summary_tail:
            detail = f"{detail}｜{summary_tail}"
    replacements = (
        (r"^注册最大尝试次数\s*[:：]\s*", "最大尝试="),
        (r"^registration_policy\s*", ""),
        (r"^已分配独立浏览器指纹\s*[:：]\s*", ""),
        (r"^使用 Helper Ready API 出池$", "渠道=HME Helper API"),
        (r"^Helper 已领取别名\s*[:：]\s*", "别名="),
        (r"\s+lease=", "｜lease="),
        (r"[，,]\s*监听转发箱\s+", "｜监听转发箱="),
        (r"\s+mailbox_id=", "｜mailbox_id="),
        (r"^复用远端邮箱\s*[:：]\s*", "转发箱="),
        (r"^TempMail 转发箱命中验证码\s*[:：].*$", "来源=TempMail转发箱"),
        (r"^邮箱已获取\s*(?:｜|\|)?\s*", ""),
        (r"^验证码已收到\s*(?:｜|\|)?\s*", ""),
        (r"^验证码已提交\s*(?:｜|\|)?\s*", ""),
        (r"^注册密码已提交\s*(?:｜|\|)?\s*", ""),
        (r"^about_you 资料已提交\s*(?:｜|\|)?\s*", ""),
        (r"^账号已保存\s*(?:｜|\|)?\s*", ""),
        (r"^等待验证码\s*(?:｜|\|)?\s*", ""),
        (r"^等待邮箱验证码\s*[:：]\s*(?:\[REDACTED_OTP\]\s*)?", "类型="),
        (r"^验证码已获取\s*[:：]\s*", "类型="),
        (r"^步骤\s*[12]/2\s*[:：]\s*", ""),
        (r"^开始自动同步外部系统\s*[，,]\s*", "同步模式=自动｜"),
        (r"^Helper 已提交成功\s*[:：]\s*", "邮箱回写="),
        (r"^跳过上传\s*[:：]\s*", "原因="),
        (r"^ChatGPT 注册核心链路\s+proxy=", "注册核心代理="),
        (r"^执行 any-auto 注册运输层\s+", "运输层=any-auto｜"),
        (r"^启动 any-auto 浏览器注册运输层\s+executor=([^\s]+)\s*\(Camoufox 整段：邮箱 -> OTP -> about_you -> Web Session\)$", r"运输层=any-auto 浏览器｜executor=\1｜范围=邮箱→OTP→资料→Web Session"),
        (r"^any-auto 注册运输层成功\s+", ""),
        (r"^邮箱入口已提交$", "节点=邮箱入口"),
        (r"^注册验证码已提交(?:｜|\s*)", ""),
        (r"^about_you 资料已提交(?:｜|\s*)", "资料=about_you｜"),
        (r"^OpenAI 账号创建完成$", "OpenAI账号=已创建"),
        (r"^开始获取 ChatGPT Web Session$", "会话=ChatGPT Web Session"),
        (r"^ChatGPT Web Session 获取成功(?:｜|\s*)", ""),
        (r"^Token 提取完成(?:｜|\s*)", ""),
        (r"^any-auto 已返回 AccessToken/Session[，,]?\s*跳过二次 OAuth recovery$", "AT/Session=已获取｜二次OAuth=跳过"),
        (r"^注册流程成功结束!?$", "开户与会话材料=完整"),
        (r"^Token 提取完成!?$", "AccessToken=已获取｜SessionToken=已获取｜Cookie=已获取"),
        (r"^Plus 额度验证已关闭[，,]\s*跳过订阅链接生成和 amount 校验$", "项目=Plus额度/订阅链接｜原因=配置关闭｜影响=订阅链接和金额校验"),
        (r"^预检通过\s*[:：]\s*候选\s+(\d+)\s+个\s*\(首个\s+source=([^\)]+)\)$", r"候选=\1｜首选来源=\2"),
    )
    for pattern, replacement in replacements:
        detail = re.sub(pattern, replacement, detail, flags=re.I)

    detail = re.sub(r"\s+", " ", detail).strip(" ｜|，,；;!。")
    normalized = _normalize_registration_key_values(detail)
    if normalized:
        return status, normalized
    return status, ""


def _format_registration_timeline_body(
    message: Any,
    *,
    stage_tag: str,
    debug: bool = False,
) -> str:
    if debug:
        body = redact_log_text(message).strip()
        if body.upper().startswith("[DEBUG]"):
            body = body[len("[DEBUG]") :].strip()
        if stage_tag and body:
            return f"{stage_tag} {body}"
        return stage_tag or body
    status, detail = _registration_status_and_detail(message)
    if detail:
        return f"{stage_tag} {status}｜{detail}"
    return f"{stage_tag} {status}".rstrip()


def format_task_timeline_log(
    task: str,
    message: str = "",
    *,
    item_index: int | None = None,
    item_total: int | None = None,
    email: str = "",
    account_id: int | str | None = None,
    phone: str = "",
    phone_index: int | str | None = None,
    phone_total: int | str | None = None,
    stage_index: int | None = None,
    stage_total: int | None = None,
    phase_label: str = "",
    success_slot: int | None = None,
    success_total: int | None = None,
    debug: bool = False,
) -> str:
    """Build a stable human-facing task timeline log line.

    The output intentionally stays plain text so the current frontend keeps
    working, while the wording becomes much more predictable:

    Registration lines intentionally use only the success-slot and stage tags:
    ``[1/3][步骤05/09 邮箱验证] 验证码已收到｜长度=6``.
    """

    task_name = str(task or "").strip()
    phone_binding_task = task_name in {"手机绑定", "手机号绑定"}
    registration_task = task_name in {"ChatGPT注册", "ChatGPT 注册"}
    expose_phone = False
    expose_otp = False

    def _pad(value: int, total_value: int) -> str:
        width = max(2, len(str(max(int(total_value or 0), 0))))
        return f"{int(value or 0):0{width}d}"

    tags: list[str] = []
    task_label = redact_log_text(task, expose_phone=expose_phone, expose_otp=expose_otp).strip()
    if task_label and not phone_binding_task and not registration_task:
        tags.append(task_label)

    normalized_index = int(item_index or 0)
    normalized_total = int(item_total or 0)
    if phone_binding_task:
        normalized_stage_index = int(stage_index or 0)
        normalized_stage_total = int(stage_total or 0)
        tags.append("手机号绑定")
        if normalized_index > 0 and normalized_total > 0:
            tags.append(f"账号 {normalized_index}/{normalized_total}")
        phone_index_text = str(phone_index or "").strip()
        phone_total_text = str(phone_total or "").strip()
        if phone_index_text and phone_total_text:
            tags.append(f"号码 {phone_index_text}/{phone_total_text}")
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
        stage_tag = "".join(f"[{tag}]" for tag in tags)
        if stage_tag:
            return _format_phone_binding_timeline_body(body, stage_tag=stage_tag)
        return body

    if registration_task:
        normalized_stage_index = int(stage_index or 0)
        normalized_stage_total = int(stage_total or 0)
        resolved_success_slot = int(success_slot or normalized_index or 0)
        resolved_success_total = int(success_total or normalized_total or 0)
        if resolved_success_slot <= 0:
            # Task-level registration events happen before an account has been
            # claimed.  Keep the same stable shape without exposing an attempt
            # counter; the first potential success is the operator's next slot.
            resolved_success_slot = 1
        if resolved_success_total <= 0:
            resolved_success_total = max(resolved_success_slot, 1)
        tags.append(f"{resolved_success_slot}/{resolved_success_total}")
        normalized_phase_label = redact_log_text(phase_label).strip()
        if normalized_stage_index > 0 and normalized_stage_total > 0:
            stage_text = (
                f"步骤{_pad(normalized_stage_index, normalized_stage_total)}/"
                f"{_pad(normalized_stage_total, normalized_stage_total)}"
            )
            if normalized_phase_label:
                stage_text = f"{stage_text} {normalized_phase_label}"
            tags.append(stage_text)
        stage_tag = "".join(f"[{tag}]" for tag in tags)
        formatted = _format_registration_timeline_body(
            message,
            stage_tag=stage_tag,
            debug=bool(debug),
        )
        if debug and formatted and not formatted.lstrip().upper().startswith("[DEBUG]"):
            return f"[DEBUG]{formatted}"
        return formatted

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
