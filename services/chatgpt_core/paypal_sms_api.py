"""Local SMS API polling helpers for the ChatGPT PayPal binding flow.

The plus.iceaix.com job can emit an OTP-needed event, but third-party SMS APIs
are not guaranteed to be parseable by that external service.  This module keeps
the API request and PayPal-code parsing local to this app.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from core.safe_http import open_http_url

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - very old Python fallback
    ZoneInfo = None  # type: ignore[assignment]


LogFn = Callable[[str], None]
StopCheckFn = Callable[[], None]

DEFAULT_SMS_TIMEZONE_NAME = "Asia/Shanghai"
DEFAULT_STABILITY_SECONDS = 10
DEFAULT_POLL_INTERVAL_SECONDS = 2

_CODE_FIELD_RE = re.compile(r"(?:^|[|&?\s])code\s*=\s*(\d{4,8})(?:\b|$)", re.IGNORECASE)
_PAYPAL_CODE_RES = [
    re.compile(r"PayPal\s*验证代码是\s*[：:]?\s*(\d{4,8})", re.IGNORECASE),
    re.compile(r"PayPal\s*验证码\s*[：:]?\s*(\d{4,8})", re.IGNORECASE),
    re.compile(r"验证码(?:是|为)?\s*[：:]?\s*(\d{4,8})", re.IGNORECASE),
    re.compile(r"verification\s+code(?:\s+is)?\s*[：:]?\s*(\d{4,8})", re.IGNORECASE),
]
_ANY_SIX_DIGIT_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_CHINESE_TIME_RE = re.compile(
    r"接码时间\s*[：:]\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s+"
    r"(\d{1,2}):(\d{1,2}):(\d{1,2})"
)
_ISO_TIME_FIELD_RE = re.compile(
    r"(?:received_at|created_at|time|timestamp)\s*=\s*([0-9T:\-.+Z ]{10,32})",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ParsedSmsOtp:
    code: str
    received_at: datetime | None = None
    record_id: str = ""
    phone: str = ""
    duplicate: bool | None = None
    existing: bool | None = None
    raw: str = ""

    @property
    def stable_key(self) -> tuple[str, str, str]:
        received = self.received_at.isoformat() if self.received_at else ""
        return (self.code, self.record_id, received)


def _sms_timezone():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(DEFAULT_SMS_TIMEZONE_NAME)
        except Exception:
            pass
    return timezone(timedelta(hours=8))


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_bool(value: str) -> bool | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _extract_fields(parts: list[str], text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for segment in parts[1:]:
        if "=" not in segment:
            continue
        key, value = segment.split("=", 1)
        key = key.strip()
        if not key:
            continue
        fields[key] = value.strip()
    # Be tolerant of `a=b&c=d` or JSON-ish wrappers in custom APIs.
    for key, value in urllib.parse.parse_qsl(text, keep_blank_values=True):
        if key and key not in fields:
            fields[key] = value.strip()
    return fields


def _extract_code(text: str, fields: dict[str, str]) -> str:
    code = re.sub(r"\D", "", str(fields.get("code") or ""))
    if 4 <= len(code) <= 8:
        return code

    field_match = _CODE_FIELD_RE.search(text)
    if field_match:
        return field_match.group(1)

    for pattern in _PAYPAL_CODE_RES:
        match = pattern.search(text)
        if match:
            return match.group(1)

    match = _ANY_SIX_DIGIT_RE.search(text)
    if match:
        return match.group(1)
    return ""


def _parse_received_at(text: str, fields: dict[str, str]) -> datetime | None:
    tz = _sms_timezone()
    match = _CHINESE_TIME_RE.search(text)
    if match:
        year, month, day, hour, minute, second = [int(item) for item in match.groups()]
        return datetime(year, month, day, hour, minute, second, tzinfo=tz).astimezone(timezone.utc)

    for key in ("received_at", "created_at", "time", "timestamp"):
        raw = str(fields.get(key) or "").strip()
        if not raw:
            continue
        parsed = _parse_loose_datetime(raw)
        if parsed is not None:
            return parsed

    match = _ISO_TIME_FIELD_RE.search(text)
    if match:
        return _parse_loose_datetime(match.group(1).strip())
    return None


def _parse_loose_datetime(raw: str) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    tz = _sms_timezone()
    candidates = [value]
    if value.endswith("Z"):
        candidates.append(value[:-1] + "+00:00")
    if " " in value and "T" not in value:
        candidates.append(value.replace(" ", "T", 1))
    for item in candidates:
        try:
            parsed = datetime.fromisoformat(item)
        except Exception:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(timezone.utc)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(value, fmt)
        except Exception:
            continue
        return parsed.replace(tzinfo=tz).astimezone(timezone.utc)
    return None


def parse_sms_api_response(raw_text: str | bytes | dict[str, Any] | list[Any]) -> ParsedSmsOtp | None:
    """Parse a PayPal OTP response from a local SMS API.

    Supports the linlinflow format, for example:
    `yes|您的PayPal验证代码是：718704。接码时间:2026年06月09日 07:08:58|recordId=988|phone=080...|code=718704`.
    """
    if isinstance(raw_text, bytes):
        text = raw_text.decode("utf-8", errors="replace")
    elif isinstance(raw_text, (dict, list)):
        text = json.dumps(raw_text, ensure_ascii=False)
    else:
        text = str(raw_text or "")
    text = text.strip()
    if not text:
        return None

    # Some APIs return JSON containing the actual SMS text/code.
    if text[:1] in {"{", "["}:
        try:
            parsed_json = json.loads(text)
        except Exception:
            parsed_json = None
        extracted = _flatten_json_values(parsed_json)
        if extracted:
            text_for_parse = "|".join(extracted)
        else:
            text_for_parse = text
    else:
        text_for_parse = text

    parts = text_for_parse.split("|")
    fields = _extract_fields(parts, text_for_parse)
    code = _extract_code(text_for_parse, fields)
    if not code:
        return None

    return ParsedSmsOtp(
        code=code,
        received_at=_parse_received_at(text_for_parse, fields),
        record_id=str(fields.get("recordId") or fields.get("record_id") or fields.get("id") or "").strip(),
        phone=str(fields.get("phone") or fields.get("mobile") or fields.get("msisdn") or "").strip(),
        duplicate=_parse_bool(fields.get("duplicate", "")),
        existing=_parse_bool(fields.get("existing", "")),
        raw=text,
    )


def _flatten_json_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for key in ("text", "message", "sms", "content", "body", "raw", "code", "phone", "recordId", "received_at", "created_at"):
            if key in value and value[key] is not None:
                values.append(f"{key}={value[key]}" if key in {"code", "phone", "recordId", "received_at", "created_at"} else str(value[key]))
        for item in value.values():
            if isinstance(item, (dict, list)):
                values.extend(_flatten_json_values(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(_flatten_json_values(item))
    elif value is not None:
        values.append(str(value))
    return values


def mask_sms_code(code: str) -> str:
    digits = re.sub(r"\D", "", str(code or ""))
    if not digits:
        return "-"
    if len(digits) <= 3:
        return "*" * len(digits)
    return "*" * (len(digits) - 3) + digits[-3:]


def format_sms_time(value: datetime | None) -> str:
    if value is None:
        return "-"
    return _ensure_aware_utc(value).astimezone(_sms_timezone()).strftime("%Y-%m-%d %H:%M:%S %Z")


def describe_sms_api_url(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return "url=invalid"
    query = urllib.parse.parse_qs(parsed.query)
    bits = [f"host={parsed.netloc}"]
    for key in ("phone", "wait", "pf"):
        value = (query.get(key) or [""])[0]
        if value:
            bits.append(f"{key}={value}")
    return " ".join(bits)


def poll_paypal_sms_otp(
    sms_api_url: str,
    *,
    wait_started_at: datetime,
    expected_phone: str = "",
    exclude_codes: set[str] | None = None,
    exclude_record_ids: set[str] | None = None,
    timeout_seconds: int = 180,
    stability_seconds: int = DEFAULT_STABILITY_SECONDS,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    log: LogFn | None = None,
    stop_check: StopCheckFn | None = None,
    stop_event: Any | None = None,
    reveal_code_in_log: bool = False,
) -> ParsedSmsOtp | None:
    """Poll a local SMS API until a new PayPal OTP is stable.

    A code is accepted only when:
    - a PayPal OTP code can be parsed locally;
    - the SMS response contains a received time and it is after the OTP wait step;
    - its record/code has not already been used by this task;
    - it remains the latest candidate through the stability observation window.
    """
    url = str(sms_api_url or "").strip()
    if not url:
        return None
    try:
        parsed_sms_url = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if (
        parsed_sms_url.scheme.lower() not in {"http", "https"}
        or not parsed_sms_url.hostname
        or parsed_sms_url.username is not None
        or parsed_sms_url.password is not None
        or parsed_sms_url.fragment
    ):
        _log(log, "收码 API URL 不是允许的 HTTP(S) 地址，已拒绝")
        return None

    wait_started_utc = _ensure_aware_utc(wait_started_at)
    excluded_codes = {re.sub(r"\D", "", str(item or "")) for item in (exclude_codes or set()) if str(item or "").strip()}
    excluded_records = {str(item or "").strip() for item in (exclude_record_ids or set()) if str(item or "").strip()}
    timeout_value = max(int(timeout_seconds or 180), 1)
    stability_value = max(int(stability_seconds or DEFAULT_STABILITY_SECONDS), 0)
    poll_interval = max(float(poll_interval_seconds or DEFAULT_POLL_INTERVAL_SECONDS), 0.5)
    search_deadline = time.monotonic() + timeout_value
    overall_deadline = search_deadline
    candidate: ParsedSmsOtp | None = None
    stable_deadline = 0.0
    ignored_logged: set[tuple[str, str, str]] = set()
    last_error_log_at = 0.0

    _log(
        log,
        "开始本地轮询: "
        f"{describe_sms_api_url(url)} wait_started={format_sms_time(wait_started_utc)} "
        f"timeout={timeout_value}s stability={stability_value}s",
    )

    while time.monotonic() < overall_deadline:
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            _log(log, "本地轮询已停止")
            return None
        if stop_check is not None:
            stop_check()

        now = time.monotonic()
        if candidate is not None and stable_deadline > 0 and now >= stable_deadline:
            _log(
                log,
                "验证码稳定观察完成: "
                f"code={_display_code(candidate.code, reveal_code_in_log)} "
                f"recordId={candidate.record_id or '-'} received_at={format_sms_time(candidate.received_at)}",
            )
            return candidate

        request_url = url
        if candidate is not None:
            request_url = _with_wait_param(url, max(1, min(int(poll_interval), max(int(stable_deadline - now), 1))))
        request_timeout = _request_timeout_seconds(request_url, max(overall_deadline - now, 1.0), candidate_seen=candidate is not None)

        try:
            raw = _fetch_text(request_url, timeout_seconds=request_timeout)
        except Exception as exc:
            if time.monotonic() - last_error_log_at >= 10:
                _log(log, f"接口请求失败，继续轮询: {exc}")
                last_error_log_at = time.monotonic()
            _sleep_interruptible(poll_interval, stop_event=stop_event, stop_check=stop_check)
            continue

        parsed = parse_sms_api_response(raw)
        if parsed is None:
            if time.monotonic() - last_error_log_at >= 15:
                _log(log, "接口响应暂未解析到 PayPal 验证码，继续轮询")
                last_error_log_at = time.monotonic()
            _sleep_interruptible(poll_interval, stop_event=stop_event, stop_check=stop_check)
            continue

        reject_reason = _reject_reason(
            parsed,
            wait_started_utc=wait_started_utc,
            expected_phone=expected_phone,
            excluded_codes=excluded_codes,
            excluded_records=excluded_records,
        )
        if reject_reason:
            key = (parsed.code, parsed.record_id, reject_reason)
            if key not in ignored_logged:
                ignored_logged.add(key)
                _log(
                    log,
                    "跳过短信: "
                    f"reason={reject_reason} code={_display_code(parsed.code, reveal_code_in_log)} "
                    f"recordId={parsed.record_id or '-'} received_at={format_sms_time(parsed.received_at)} "
                    f"phone={parsed.phone or '-'}",
                )
            _sleep_interruptible(poll_interval, stop_event=stop_event, stop_check=stop_check)
            continue

        if _is_newer_candidate(parsed, candidate):
            candidate = parsed
            stable_deadline = time.monotonic() + stability_value
            overall_deadline = max(overall_deadline, stable_deadline + 1.0)
            _log(
                log,
                "捕获候选验证码，继续观察防变化: "
                f"code={_display_code(parsed.code, reveal_code_in_log)} "
                f"recordId={parsed.record_id or '-'} received_at={format_sms_time(parsed.received_at)} "
                f"observe={stability_value}s duplicate={parsed.duplicate} existing={parsed.existing}",
            )

        _sleep_interruptible(poll_interval, stop_event=stop_event, stop_check=stop_check)

    if candidate is not None:
        _log(
            log,
            "轮询到候选验证码但稳定观察超时，采用最后候选: "
            f"code={_display_code(candidate.code, reveal_code_in_log)} recordId={candidate.record_id or '-'}",
        )
        return candidate
    _log(log, "本地轮询超时，未取得满足时间条件的新 PayPal OTP")
    return None


def _reject_reason(
    parsed: ParsedSmsOtp,
    *,
    wait_started_utc: datetime,
    expected_phone: str,
    excluded_codes: set[str],
    excluded_records: set[str],
) -> str:
    if not parsed.received_at:
        return "缺少接码时间"
    received_utc = _ensure_aware_utc(parsed.received_at)
    # The SMS API timestamp is second-precision.  We compare to the exact UTC
    # time passed by the caller; callers can floor that value if they want a
    # looser same-second policy.
    if received_utc <= wait_started_utc:
        return "接码时间早于或等于等待开始时间"
    code_digits = re.sub(r"\D", "", parsed.code)
    if code_digits and code_digits in excluded_codes:
        return "验证码已在本任务使用过"
    if parsed.record_id and parsed.record_id in excluded_records:
        return "recordId已在本任务使用过"
    if expected_phone and parsed.phone and not _phones_match(expected_phone, parsed.phone):
        return "手机号不匹配"
    return ""


def _phones_match(expected: str, actual: str) -> bool:
    expected_digits = re.sub(r"\D", "", str(expected or ""))
    actual_digits = re.sub(r"\D", "", str(actual or ""))
    if not expected_digits or not actual_digits:
        return True
    if expected_digits == actual_digits:
        return True
    min_len = min(len(expected_digits), len(actual_digits), 10)
    if min_len >= 8 and expected_digits[-min_len:] == actual_digits[-min_len:]:
        return True
    # Japan-style +81 80... vs local 080...
    if expected_digits.startswith("81") and actual_digits.startswith("0"):
        return expected_digits[2:] == actual_digits[1:] or expected_digits[-10:] == actual_digits[-10:]
    return False


def _is_newer_candidate(parsed: ParsedSmsOtp, current: ParsedSmsOtp | None) -> bool:
    if current is None:
        return True
    if parsed.stable_key == current.stable_key:
        return False
    parsed_time = parsed.received_at or datetime.min.replace(tzinfo=timezone.utc)
    current_time = current.received_at or datetime.min.replace(tzinfo=timezone.utc)
    if _ensure_aware_utc(parsed_time) > _ensure_aware_utc(current_time):
        return True
    if parsed.record_id and parsed.record_id != current.record_id:
        return True
    return parsed.code != current.code


def _display_code(code: str, reveal: bool) -> str:
    return str(code or "-") if reveal else mask_sms_code(code)


def _log(log: LogFn | None, message: str) -> None:
    if log is not None:
        log(message)


def _fetch_text(url: str, *, timeout_seconds: float) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "auto-chatgpt-paypal-sms/1.0",
            "Accept": "text/plain, application/json, */*",
        },
        method="GET",
    )
    try:
        with open_http_url(
            request,
            timeout=max(float(timeout_seconds or 10), 1.0),
        ) as response:
            body = response.read(1024 * 64)
    except urllib.error.HTTPError as exc:
        body = exc.read(4096)
        text = body.decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {text[:200]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc)) from exc
    return body.decode("utf-8", errors="replace")


def _request_timeout_seconds(url: str, remaining_seconds: float, *, candidate_seen: bool) -> float:
    wait_param = _query_wait_seconds(url)
    remaining = max(float(remaining_seconds or 1), 1.0)
    if candidate_seen:
        return min(max(wait_param + 2.0, 3.0), max(remaining, 1.0))
    if wait_param > 0:
        return min(max(wait_param + 8.0, 10.0), remaining + 5.0)
    return min(15.0, remaining + 5.0)


def _query_wait_seconds(url: str) -> float:
    try:
        parsed = urllib.parse.urlparse(url)
        value = (urllib.parse.parse_qs(parsed.query).get("wait") or [""])[0]
        return max(float(value or 0), 0.0)
    except Exception:
        return 0.0


def _with_wait_param(url: str, wait_seconds: int) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if not any(key == "wait" for key, _ in query):
            return url
        next_query = [(key, str(max(int(wait_seconds or 1), 1)) if key == "wait" else value) for key, value in query]
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(next_query)))
    except Exception:
        return url


def _sleep_interruptible(seconds: float, *, stop_event: Any | None, stop_check: StopCheckFn | None) -> None:
    deadline = time.monotonic() + max(float(seconds or 0), 0.0)
    while time.monotonic() < deadline:
        if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
            return
        if stop_check is not None:
            stop_check()
        time.sleep(min(0.25, max(deadline - time.monotonic(), 0.0)))
