from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests

from core.task_runtime import TaskInterruption
from services.chatgpt_core.task_logging import mask_phone_for_log, redact_log_text

from smstome_tool import (
    PhoneEntry,
    get_unused_phone,
    mark_phone_blacklisted,
    parse_country_slugs,
    update_global_phone_list,
    wait_for_otp,
)


def _to_positive_int(value, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return default
    return parsed if parsed >= minimum else default


def _to_bool(value, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "是", "开启", "启用"}:
        return True
    if text in {"0", "false", "no", "off", "n", "否", "关闭", "禁用"}:
        return False
    return default


_BEIJING_TZ = timezone(timedelta(hours=8))


def _parse_uploaded_sms_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        return numeric / 1000 if numeric > 1_000_000_000_000 else numeric

    text = str(value or "").strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        if numeric <= 0:
            return None
        return numeric / 1000 if numeric > 1_000_000_000_000 else numeric

    iso_text = text.replace(" ", "T", 1) if " " in text and "T" not in text else text
    if iso_text.endswith("Z"):
        iso_text = iso_text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_BEIJING_TZ)
    return parsed.astimezone(timezone.utc).timestamp()


def _prefix_hint(phone: str, width: int = 7) -> str:
    value = str(phone or "").strip()
    return value[: min(len(value), width)] if value else ""


def _safe_response_snippet(value: Any, limit: int = 300) -> str:
    return redact_log_text(str(value or ""))[:limit]


class SMSToMePhoneService:
    def __init__(self, config: Optional[dict] = None, log_fn: Optional[Callable[[str], None]] = None):
        self.config = dict(config or {})
        self.log_fn = log_fn or (lambda _msg: None)
        self.stop_checker = self.config.get("_task_stop_checker")
        self.cookie_header = str(self.config.get("smstome_cookie", "") or "").strip() or None
        self.country_slugs = parse_country_slugs(self.config.get("smstome_country_slugs"))
        self.global_file = Path(str(self.config.get("smstome_global_file") or "smstome_all_numbers.txt"))
        self.used_numbers_dir = Path(str(self.config.get("smstome_used_numbers_dir") or "smstome_used"))
        self.task_name = str(self.config.get("smstome_task_name") or "chatgpt_add_phone").strip() or "chatgpt_add_phone"
        self.max_attempts = _to_positive_int(self.config.get("smstome_phone_attempts"), 3)
        self.max_resend_attempts = 1
        self.resend_interval_seconds = 0
        self.otp_timeout_seconds = _to_positive_int(self.config.get("smstome_otp_timeout_seconds"), 45, minimum=10)
        self.poll_interval_seconds = _to_positive_int(self.config.get("smstome_poll_interval_seconds"), 5, minimum=1)
        self.sync_max_pages_per_country = _to_positive_int(
            self.config.get("smstome_sync_max_pages_per_country"),
            5,
        )

    def _check_stop(self) -> None:
        if callable(self.stop_checker):
            self.stop_checker()

    @property
    def enabled(self) -> bool:
        return self._has_pool_file() or bool(self.cookie_header)

    def prefix_hint(self, phone: str) -> str:
        return _prefix_hint(phone)

    def _has_pool_file(self) -> bool:
        try:
            return self.global_file.exists() and self.global_file.stat().st_size > 0
        except OSError:
            return False

    def ensure_pool_ready(self) -> None:
        self._check_stop()
        if self._has_pool_file():
            return
        if not self.cookie_header:
            raise RuntimeError("未找到 SMSToMe 号码池文件，且未配置 smstome_cookie")

        self.log_fn("SMSToMe 号码池不存在，开始自动同步...")
        count = update_global_phone_list(
            cookie_header=self.cookie_header,
            countries=self.country_slugs or None,
            output_path=self.global_file,
            max_pages_per_country=self.sync_max_pages_per_country,
        )
        if count <= 0:
            raise RuntimeError("SMSToMe 号码池同步后为空")
        self.log_fn(f"SMSToMe 号码池同步完成，共 {count} 个号码")

    def acquire_phone(self, *, exclude_prefixes: Optional[Iterable[str]] = None, **_kwargs) -> Optional[PhoneEntry]:
        self._check_stop()
        self.ensure_pool_ready()
        return get_unused_phone(
            self.task_name,
            country_slug=self.country_slugs or None,
            global_file=self.global_file,
            used_numbers_dir=self.used_numbers_dir,
            exclude_prefixes=exclude_prefixes,
        )

    def mark_blacklisted(self, phone: str, *, reason: str = "") -> None:
        mark_phone_blacklisted(self.task_name, phone, used_numbers_dir=self.used_numbers_dir)

    def mark_sms_sent(self, _entry: PhoneEntry) -> None:
        return None

    def request_next_code(self, _entry: PhoneEntry) -> bool:
        return False

    def complete(self, _entry: PhoneEntry) -> None:
        return None

    def cancel(self, _entry: PhoneEntry, *, reason: str = "") -> None:
        return None

    def wait_for_code(self, entry: PhoneEntry, *, timeout: Optional[int] = None) -> Optional[str]:
        self._check_stop()
        wait_seconds = _to_positive_int(timeout, self.otp_timeout_seconds, minimum=10)
        return wait_for_otp(
            entry,
            cookie_header=self.cookie_header,
            timeout=wait_seconds,
            poll_interval=self.poll_interval_seconds,
            trace=lambda message: self.log_fn(f"[SMSToMe] {message}"),
            stop_checker=self._check_stop,
            raise_on_timeout=False,
        )


@dataclass(frozen=True)
class LocalGatewayPhoneEntry:
    country_slug: str
    phone: str
    detail_url: str
    activation_id: str
    provider: str = ""
    provider_activation_id: str = ""


class LocalPhoneGatewayService:
    def __init__(self, config: Optional[dict] = None, log_fn: Optional[Callable[[str], None]] = None):
        self.config = dict(config or {})
        self.log_fn = log_fn or (lambda _msg: None)
        self.stop_checker = self.config.get("_task_stop_checker")
        self.base_url = str(self.config.get("local_phone_gateway_url") or "").strip().rstrip("/")
        self.token = str(self.config.get("local_phone_gateway_token") or "").strip()
        self.service_alias = str(self.config.get("local_phone_gateway_service_alias") or "chatgpt").strip() or "chatgpt"
        self.consumer = str(self.config.get("local_phone_gateway_consumer") or "any-auto-register-local").strip()
        self.auto_acquire_enabled = _to_bool(
            self.config.get("local_phone_gateway_auto_acquire_enabled"),
            True,
        )
        self.max_attempts = _to_positive_int(
            self.config.get("local_phone_gateway_max_attempts") or self.config.get("smstome_phone_attempts"),
            3,
        )
        self.max_resend_attempts = _to_positive_int(
            self.config.get("local_phone_gateway_max_resend_attempts"),
            20,
        )
        self.resend_interval_seconds = _to_positive_int(
            self.config.get("local_phone_gateway_resend_interval_seconds"),
            30,
            minimum=0,
        )
        self.queue_timeout_seconds = _to_positive_int(
            self.config.get("local_phone_gateway_queue_timeout_seconds"),
            3600,
            minimum=30,
        )
        self.otp_timeout_seconds = _to_positive_int(
            self.config.get("local_phone_gateway_timeout_seconds") or self.config.get("smstome_otp_timeout_seconds"),
            180,
            minimum=10,
        )
        self.poll_interval_seconds = _to_positive_int(
            self.config.get("local_phone_gateway_poll_interval_seconds") or self.config.get("smstome_poll_interval_seconds"),
            5,
            minimum=1,
        )
        self._entries_by_phone: dict[str, LocalGatewayPhoneEntry] = {}

    def _check_stop(self) -> None:
        if callable(self.stop_checker):
            self.stop_checker()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    def prefix_hint(self, phone: str) -> str:
        return _prefix_hint(phone)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        timeout: Optional[int] = None,
        check_stop: bool = True,
    ) -> dict:
        if check_stop:
            self._check_stop()
        if not self.enabled:
            raise RuntimeError("本地接码网关未配置 local_phone_gateway_url/local_phone_gateway_token")
        try:
            resp = requests.request(
                method,
                self._url(path),
                headers=self._headers(),
                json=json_body,
                timeout=timeout or 30,
            )
        except Exception as exc:
            raise RuntimeError(f"本地接码网关请求失败: {exc}") from exc
        try:
            data = resp.json()
        except Exception:
            data = {"message": _safe_response_snippet(resp.text, 300)}
        if resp.status_code >= 400 or not bool(data.get("ok", resp.status_code < 400)):
            message = str(
                data.get("message")
                or data.get("detail")
                or data.get("error")
                or _safe_response_snippet(resp.text, 300)
                or "本地接码网关返回失败"
            )
            raise RuntimeError(redact_log_text(message))
        return data

    def acquire_phone(
        self,
        *,
        exclude_prefixes: Optional[Iterable[str]] = None,
        email: str = "",
        account_id: int = 0,
        task_id: str = "",
        purpose: str = "chatgpt_add_phone",
    ) -> Optional[LocalGatewayPhoneEntry]:
        base_body = {
            "consumer": self.consumer,
            "task_id": str(task_id or ""),
            "account_id": int(account_id or 0),
            "email": str(email or ""),
            "purpose": purpose,
            "service_alias": self.service_alias,
            "timeout_seconds": self.otp_timeout_seconds,
            "exclude_prefixes": list(exclude_prefixes or []),
            "auto_acquire": self.auto_acquire_enabled,
        }
        deadline = time.monotonic() + self.queue_timeout_seconds
        last_message = ""
        data = {}
        while True:
            self._check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.log_fn(
                    f"[接码网关] 串行通道等待超时({self.queue_timeout_seconds}s): {last_message or '暂无可用号码'}"
                )
                return None
            window = max(1, int(min(5, remaining)))
            body = dict(base_body)
            body["queue_timeout_seconds"] = window
            data = self._request(
                "POST",
                "/api/v1/autogpt/phone-session/acquire",
                json_body=body,
                timeout=window + 10,
            )
            if data.get("claimed"):
                break
            message = str(data.get("message") or "").strip()
            if message and message != last_message:
                self.log_fn(f"[接码网关] 串行通道暂无可用号码: {message}")
                last_message = message
            if not data.get("queued"):
                return None
            if not self.auto_acquire_enabled:
                return None
        if not data.get("claimed"):
            self.log_fn(f"[接码网关] 串行通道暂无可用号码: {data.get('message') or ''}")
            return None
        activation_id = str(data.get("activation_id") or "").strip()
        phone = str(data.get("phone") or "").strip()
        if not activation_id or not phone:
            raise RuntimeError("本地接码网关未返回有效 activation_id/phone")
        entry = LocalGatewayPhoneEntry(
            country_slug=str(data.get("country") or data.get("country_id") or "local_gateway"),
            phone=phone,
            detail_url=f"{self.base_url}/api/v1/activations/{activation_id}",
            activation_id=activation_id,
            provider=str(data.get("provider") or ""),
            provider_activation_id=str(data.get("provider_activation_id") or ""),
        )
        self._entries_by_phone[phone] = entry
        source_map = {
            "reuse_active": "复用当前通道号码",
            "reserved_pool": "待用池领取",
            "new_number": "新取号",
        }
        source = source_map.get(str(data.get("source") or ""), str(data.get("source") or "串行通道"))
        queued_message = str(data.get("queued_message") or "").strip()
        if queued_message:
            self.log_fn(f"[接码网关] 串行通道已排队等待: {queued_message}")
        self.log_fn(
            f"[接码网关] 已取号({source}): activation_id={activation_id}, provider={entry.provider or '-'}"
        )
        return entry

    def mark_sms_sent(self, entry: LocalGatewayPhoneEntry) -> None:
        self._check_stop()
        self._request("POST", f"/api/v1/activations/{entry.activation_id}/sent", json_body={"detail": "OpenAI add-phone/send ok"})

    def record_reuse(self, entry: LocalGatewayPhoneEntry, **kwargs) -> None:
        self._check_stop()
        body = {
            "consumer": self.consumer,
            "task_id": str(kwargs.get("task_id") or ""),
            "account_id": int(kwargs.get("account_id") or 0),
            "email": str(kwargs.get("email") or ""),
            "purpose": str(kwargs.get("purpose") or "chatgpt_add_phone"),
            "service_alias": self.service_alias,
        }
        self._request(
            "POST",
            f"/api/v1/activations/{entry.activation_id}/use",
            json_body=body,
        )

    def request_next_code(self, entry: LocalGatewayPhoneEntry) -> bool:
        self._check_stop()
        try:
            self._request("POST", f"/api/v1/activations/{entry.activation_id}/retry", json_body={"reason": "OpenAI resend"})
            return True
        except TaskInterruption:
            raise
        except Exception as exc:
            self.log_fn(f"[接码网关] 请求下一条短信失败: {exc}")
            return False

    def complete(self, entry: LocalGatewayPhoneEntry) -> None:
        try:
            data = self._request(
                "POST",
                f"/api/v1/activations/{entry.activation_id}/complete",
                json_body={"reason": "OpenAI phone OTP validated"},
                check_stop=False,
            )
            if data.get("reusable"):
                self.log_fn(
                    f"[接码网关] 手机号验证完成，已放回串行通道继续复用: activation_id={entry.activation_id}"
                )
        except Exception as exc:
            self.log_fn(f"[接码网关] 完成订单失败: {exc}")

    def cancel(self, entry: LocalGatewayPhoneEntry, *, reason: str = "") -> None:
        try:
            self._request(
                "POST",
                f"/api/v1/activations/{entry.activation_id}/cancel",
                json_body={"reason": reason},
                check_stop=False,
            )
        except Exception as exc:
            self.log_fn(f"[接码网关] 取消订单失败: {exc}")

    def mark_blacklisted(self, phone: str, *, reason: str = "") -> None:
        entry = self._entries_by_phone.get(str(phone or "").strip())
        if entry:
            self.cancel(entry, reason=reason or "phone blacklisted by upstream")

    def wait_for_code(self, entry: LocalGatewayPhoneEntry, *, timeout: Optional[int] = None) -> Optional[str]:
        wait_seconds = _to_positive_int(timeout, self.otp_timeout_seconds, minimum=10)
        deadline = time.monotonic() + wait_seconds
        try:
            while True:
                self._check_stop()
                remaining = int(max(0, deadline - time.monotonic()))
                if remaining <= 0:
                    return None
                window = min(max(self.poll_interval_seconds, 1), remaining)
                data = self._request(
                    "GET",
                    f"/api/v1/activations/{entry.activation_id}/code?wait_seconds={window}&poll_interval_seconds={self.poll_interval_seconds}",
                    timeout=window + 30,
                )
                code = str(data.get("code") or "").strip()
                if code:
                    return code
        except TaskInterruption:
            raise
        except Exception as exc:
            self.log_fn(f"[接码网关] 等待验证码失败: {exc}")
            return None


@dataclass(frozen=True)
class UploadedPhoneEntry:
    country_slug: str
    phone: str
    detail_url: str
    api_url: str
    raw_line: str = ""
    line_no: int = 0


_UPLOADED_PHONE_API_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.I)
_UPLOADED_SMS_PENDING_RE = re.compile(
    r"(?:^|\b)NO\s*\||暂无(?:短信|验证码|信息)|未收到|没有(?:短信|验证码)|"
    r"no\s+(?:sms|message|verification\s+code|code)|not\s+received|pending|waiting",
    re.I,
)
_UPLOADED_SMS_ERROR_RE = re.compile(
    r"(?:expired|invalid|forbidden|unauthorized|error|failed|exception|过期|失效|无效|错误|失败)",
    re.I,
)
_UPLOADED_SMS_CODE_CONTEXT_RE = re.compile(
    r"(?:验证码|验证代码|校验码|动态码|短信码|OpenAI|verification\s+code|verify\s+code|"
    r"security\s+code|one[-\s]?time\s+code|otp|code(?:\s+is)?)"
    r"[^0-9]{0,40}(\d{4,6})(?!\d)",
    re.I,
)


def _normalize_uploaded_phone(raw_phone: Any) -> str:
    text = str(raw_phone or "").strip()
    if not text:
        return ""
    has_plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    return f"+{digits}" if has_plus or text.startswith("00") else f"+{digits}"


def _split_uploaded_phone_api_line(raw_line: Any) -> tuple[str, str, str]:
    line = str(raw_line or "").strip()
    if not line:
        return "", "", ""
    match = _UPLOADED_PHONE_API_URL_RE.search(line)
    if not match:
        return "", "", "缺少有效 API URL（支持 手机号----https://...、手机号---https://... 或 手机号|https://...）"
    phone = _normalize_uploaded_phone(line[: match.start()])
    api_url = str(match.group(0) or "").strip().rstrip("，,；;")
    if not phone:
        return "", api_url, "手机号为空或格式无效"
    parsed_url = urlparse(api_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        return phone, api_url, "API URL 无效"
    return phone, api_url, ""


def parse_uploaded_phone_lines(raw_lines: Any) -> tuple[list[UploadedPhoneEntry], list[dict[str, Any]]]:
    if isinstance(raw_lines, str):
        lines = raw_lines.splitlines()
    elif isinstance(raw_lines, Iterable):
        lines = [str(item or "") for item in raw_lines]
    else:
        lines = []

    entries: list[UploadedPhoneEntry] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_line in enumerate(lines, start=1):
        line = str(raw_line or "").strip()
        if not line:
            continue
        phone, api_url, error = _split_uploaded_phone_api_line(line)
        if error:
            item = {"line": index, "raw": line, "reason": error}
            if phone:
                item["phone"] = phone
            errors.append(item)
            continue
        if phone in seen:
            errors.append({"line": index, "raw": line, "phone": phone, "reason": "手机号重复，本轮只保留第一次"})
            continue
        seen.add(phone)
        entries.append(
            UploadedPhoneEntry(
                country_slug="uploaded",
                phone=phone,
                detail_url=api_url,
                api_url=api_url,
                raw_line=f"{phone}----{api_url}",
                line_no=index,
            )
        )
    return entries, errors


def _extract_uploaded_sms_code(value: Any, *, require_context: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{4,6}", text):
        return text
    contextual = _UPLOADED_SMS_CODE_CONTEXT_RE.search(text)
    if contextual:
        return contextual.group(1)
    if require_context:
        return ""
    matches = re.findall(r"(?<!\d)(\d{6})(?!\d)", text)
    if matches:
        return matches[0]
    digits = re.sub(r"\D", "", text)
    if 4 <= len(digits) <= 6:
        return digits
    return ""


def _safe_response_text(resp: Any) -> str:
    text = getattr(resp, "text", "")
    if isinstance(text, str):
        return text
    try:
        content = getattr(resp, "content", b"")
        if isinstance(content, bytes):
            return content.decode(getattr(resp, "encoding", None) or "utf-8", "replace")
    except Exception:
        pass
    return "" if text is None else str(text)


def _response_status_code(resp: Any) -> int:
    try:
        return int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        return 0


def _parse_uploaded_sms_api_result(
    *,
    payload: dict[str, Any] | None = None,
    text: str = "",
    status_code: int = 0,
) -> dict[str, Any]:
    """Normalize uploaded phone API responses into code/pending/error semantics."""

    safe_text = str(text or "").strip()
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
    message = ""
    expired_date = ""
    code_time = ""
    raw_code: Any = ""
    parser = "generic_text"

    if isinstance(payload, dict):
        parser = "json_status"
        expired_date = str(
            data.get("expired_date")
            or payload.get("expired_date")
            or payload.get("expires_at")
            or data.get("expire_time")
            or payload.get("expire_time")
            or data.get("expireTime")
            or payload.get("expireTime")
            or ""
        ).strip()
        code_time = str(data.get("code_time") or payload.get("code_time") or payload.get("received_at") or "").strip()
        raw_code = (
            data.get("code")
            or data.get("verification_code")
            or data.get("otp")
            or payload.get("verification_code")
            or payload.get("otp")
            or ""
        )
        message = str(
            payload.get("msg")
            or payload.get("message")
            or payload.get("error")
            or data.get("message")
            or ""
        ).strip()
        top_level_code = payload.get("code")
        if not raw_code and isinstance(top_level_code, bool):
            if top_level_code is True:
                maybe_code = _extract_uploaded_sms_code(message)
                if maybe_code:
                    raw_code = message
        elif not raw_code and str(top_level_code or "").strip().lower() not in {"", "0", "false", "none", "null"}:
            maybe_code = _extract_uploaded_sms_code(top_level_code)
            if maybe_code:
                raw_code = top_level_code
        if not raw_code and message:
            maybe_code = _extract_uploaded_sms_code(message, require_context=True)
            if maybe_code:
                raw_code = message
        code = _extract_uploaded_sms_code(raw_code)
        if code:
            return {
                "status": "code",
                "code": code,
                "raw_code": str(raw_code or ""),
                "code_time": code_time,
                "expired_date": expired_date,
                "message": message or "OK",
                "parser": parser,
                "payload": payload,
                "raw_excerpt": _safe_response_snippet(safe_text or message, 240),
            }
        if str(raw_code or "").strip():
            return {
                "status": "error",
                "code": "",
                "raw_code": str(raw_code or ""),
                "code_time": code_time,
                "expired_date": expired_date,
                "message": "收码 API 返回了验证码字段，但无法提取 6 位数字验证码",
                "parser": parser,
                "payload": payload,
                "raw_excerpt": _safe_response_snippet(safe_text or str(raw_code), 240),
            }
        if status_code >= 400:
            return {
                "status": "error",
                "code": "",
                "raw_code": "",
                "code_time": code_time,
                "expired_date": expired_date,
                "message": message or f"HTTP {status_code}",
                "parser": parser,
                "payload": payload,
                "raw_excerpt": _safe_response_snippet(safe_text or message, 240),
            }
        return {
            "status": "pending",
            "code": "",
            "raw_code": "",
            "code_time": code_time,
            "expired_date": expired_date,
            "message": message or "No verification code",
            "parser": parser,
            "payload": payload,
            "raw_excerpt": _safe_response_snippet(safe_text or message, 240),
        }

    upper_text = safe_text.upper()
    if upper_text.startswith("NO|"):
        return {
            "status": "pending",
            "code": "",
            "raw_code": "",
            "code_time": "",
            "expired_date": "",
            "message": safe_text.split("|", 1)[1].strip() or "暂无短信",
            "parser": "plain_yes_no",
            "payload": {},
            "raw_excerpt": _safe_response_snippet(safe_text, 240),
        }
    if upper_text.startswith("YES|"):
        body = safe_text.split("|", 1)[1].strip()
        code = _extract_uploaded_sms_code(body)
        return {
            "status": "code" if code else "error",
            "code": code,
            "raw_code": body,
            "code_time": "",
            "expired_date": "",
            "message": body if code else "YES 响应中未提取到 4-6 位验证码",
            "parser": "plain_yes_no",
            "payload": {},
            "raw_excerpt": _safe_response_snippet(safe_text, 240),
        }

    code = _extract_uploaded_sms_code(safe_text, require_context=True)
    if code:
        return {
            "status": "code",
            "code": code,
            "raw_code": safe_text,
            "code_time": "",
            "expired_date": "",
            "message": safe_text,
            "parser": "generic_text",
            "payload": {},
            "raw_excerpt": _safe_response_snippet(safe_text, 240),
        }
    if _UPLOADED_SMS_PENDING_RE.search(safe_text):
        return {
            "status": "pending",
            "code": "",
            "raw_code": "",
            "code_time": "",
            "expired_date": "",
            "message": safe_text or "暂无短信",
            "parser": "generic_text",
            "payload": {},
            "raw_excerpt": _safe_response_snippet(safe_text, 240),
        }
    if status_code >= 400 or _UPLOADED_SMS_ERROR_RE.search(safe_text):
        return {
            "status": "error",
            "code": "",
            "raw_code": safe_text,
            "code_time": "",
            "expired_date": "",
            "message": safe_text or f"HTTP {status_code}",
            "parser": "generic_text",
            "payload": {},
            "raw_excerpt": _safe_response_snippet(safe_text, 240),
        }
    return {
        "status": "pending",
        "code": "",
        "raw_code": "",
        "code_time": "",
        "expired_date": "",
        "message": safe_text or "No verification code",
        "parser": "generic_text",
        "payload": {},
        "raw_excerpt": _safe_response_snippet(safe_text, 240),
    }


class UploadedPhoneService:
    """Use operator-uploaded phone/API pairs for one real OpenAI phone binding each."""

    def __init__(self, entries: Iterable[UploadedPhoneEntry], config: Optional[dict] = None, log_fn: Optional[Callable[[str], None]] = None):
        self.entries = list(entries or [])
        self.config = dict(config or {})
        self.log_fn = log_fn or (lambda _msg: None)
        self.stop_checker = self.config.get("_task_stop_checker")
        self.max_attempts = 1
        self.max_resend_attempts = _to_positive_int(
            self.config.get("uploaded_phone_max_resend_attempts"),
            0,
            minimum=0,
        )
        self.resend_interval_seconds = _to_positive_int(
            self.config.get("uploaded_phone_resend_interval_seconds"),
            30,
            minimum=0,
        )
        self.otp_timeout_seconds = _to_positive_int(
            self.config.get("uploaded_phone_timeout_seconds")
            or self.config.get("local_phone_gateway_timeout_seconds")
            or self.config.get("smstome_otp_timeout_seconds"),
            180,
            minimum=10,
        )
        self.poll_interval_seconds = _to_positive_int(
            self.config.get("uploaded_phone_poll_interval_seconds")
            or self.config.get("local_phone_gateway_poll_interval_seconds")
            or self.config.get("smstome_poll_interval_seconds"),
            5,
            minimum=1,
        )
        self.code_time_grace_seconds = _to_positive_int(
            self.config.get("uploaded_phone_code_time_grace_seconds"),
            5,
            minimum=0,
        )
        self.validate_delay_seconds = _to_positive_int(
            self.config.get("uploaded_phone_validate_delay_seconds"),
            2,
            minimum=0,
        )
        self.current_entry: UploadedPhoneEntry | None = None
        self.completed_entries: list[UploadedPhoneEntry] = []
        self.cancelled_entries: list[tuple[UploadedPhoneEntry, str]] = []
        self.last_api_payload: dict[str, Any] = {}
        self.last_api_error = ""
        self.last_expired_date = ""
        self.last_code = ""
        self.last_code_time = ""
        self.last_code_was_extracted = False
        self.last_api_parser = ""
        self.last_api_raw_excerpt = ""
        self.last_sms_sent = False
        self.last_sms_sent_at = 0.0
        self._last_stale_code_key = ""

    def _check_stop(self) -> None:
        if callable(self.stop_checker):
            self.stop_checker()

    @property
    def enabled(self) -> bool:
        return bool(self.entries)

    def prefix_hint(self, phone: str) -> str:
        return _prefix_hint(phone)

    def acquire_phone(self, *, exclude_prefixes: Optional[Iterable[str]] = None, **_kwargs) -> Optional[UploadedPhoneEntry]:
        self._check_stop()
        if self.current_entry is None:
            return None
        phone = self.current_entry.phone
        for prefix in exclude_prefixes or []:
            if phone.startswith(str(prefix or "")):
                return None
        return self.current_entry

    def bind_entry(self, entry: UploadedPhoneEntry) -> None:
        self.current_entry = entry
        self.last_api_payload = {}
        self.last_api_error = ""
        self.last_expired_date = ""
        self.last_code = ""
        self.last_code_time = ""
        self.last_code_was_extracted = False
        self.last_api_parser = ""
        self.last_api_raw_excerpt = ""
        self.last_sms_sent = False
        self.last_sms_sent_at = 0.0
        self._last_stale_code_key = ""

    def mark_sms_sent(self, entry: UploadedPhoneEntry) -> None:
        self._check_stop()
        self.last_sms_sent = True
        self.last_sms_sent_at = time.time()
        self._last_stale_code_key = ""
        self.log_fn(f"[号码测试] OpenAI 已接受并发送验证码: {entry.phone}")

    def request_next_code(self, _entry: UploadedPhoneEntry) -> bool:
        self._check_stop()
        return True

    def complete(self, entry: UploadedPhoneEntry) -> None:
        self.completed_entries.append(entry)
        if self.current_entry == entry:
            self.current_entry = None

    def cancel(self, entry: UploadedPhoneEntry, *, reason: str = "") -> None:
        self.cancelled_entries.append((entry, str(reason or "")))
        if self.current_entry == entry:
            self.current_entry = None

    def mark_blacklisted(self, phone: str, *, reason: str = "") -> None:
        if self.current_entry and self.current_entry.phone == str(phone or "").strip():
            self.cancel(self.current_entry, reason=reason or "OpenAI rejected uploaded phone")

    def _fetch_api_poll_result(self, entry: UploadedPhoneEntry) -> dict[str, Any]:
        self._check_stop()
        try:
            resp = requests.get(entry.api_url, timeout=20)
        except Exception as exc:
            raise RuntimeError(f"收码 API 请求失败: {exc}") from exc
        status_code = _response_status_code(resp)
        response_text = _safe_response_text(resp)
        payload: dict[str, Any] | None = None
        try:
            raw_payload = resp.json()
            if isinstance(raw_payload, dict):
                payload = raw_payload
        except Exception:
            payload = None
        result = _parse_uploaded_sms_api_result(
            payload=payload,
            text=response_text,
            status_code=status_code,
        )
        if status_code >= 400 and result.get("status") != "code":
            message = str(result.get("message") or result.get("raw_excerpt") or f"HTTP {status_code}")
            raise RuntimeError(f"收码 API 返回失败: {redact_log_text(message)}")
        return result

    def wait_for_code(self, entry: UploadedPhoneEntry, *, timeout: Optional[int] = None) -> Optional[str]:
        wait_seconds = _to_positive_int(timeout, self.otp_timeout_seconds, minimum=10)
        deadline = time.monotonic() + wait_seconds
        last_message = ""
        while True:
            self._check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                poll_result = self._fetch_api_poll_result(entry)
                payload = poll_result.get("payload") if isinstance(poll_result.get("payload"), dict) else {}
                self.last_api_payload = payload
                self.last_api_parser = str(poll_result.get("parser") or "")
                self.last_api_raw_excerpt = str(poll_result.get("raw_excerpt") or "")
                self.last_api_error = ""
            except TaskInterruption:
                raise
            except Exception as exc:
                self.last_api_error = str(exc)
                self.log_fn(f"[号码测试] {entry.phone} 收码 API 异常: {redact_log_text(exc)}")
                raise

            expired_date = str(poll_result.get("expired_date") or "").strip()
            if expired_date:
                self.last_expired_date = expired_date
            status = str(poll_result.get("status") or "").strip().lower()
            raw_code = str(poll_result.get("raw_code") or "")
            code = str(poll_result.get("code") or "").strip()
            if status == "error":
                error_message = str(poll_result.get("message") or "收码 API 响应无法识别")
                self.last_api_error = error_message
                self.log_fn(f"[号码测试] {entry.phone} 收码 API 异常: {redact_log_text(error_message)}")
                raise RuntimeError(error_message)
            if code:
                code_time = str(poll_result.get("code_time") or "").strip()
                if code_time:
                    self.last_code_time = code_time
                code_timestamp = _parse_uploaded_sms_time(code_time)
                if (
                    code_timestamp is not None
                    and self.last_sms_sent_at > 0
                    and code_timestamp < self.last_sms_sent_at - float(self.code_time_grace_seconds or 0)
                ):
                    stale_key = f"{code}:{code_time}"
                    if stale_key != self._last_stale_code_key:
                        self.log_fn(
                            f"[号码测试] {entry.phone} 忽略旧验证码 "
                            f"otp={code} otp_received=true otp_length={len(code)}"
                            f"{f'，时间 {code_time}' if code_time else ''}，早于本次 OpenAI 发码"
                        )
                        self._last_stale_code_key = stale_key
                    time.sleep(min(float(self.poll_interval_seconds), max(0.1, remaining)))
                    continue
                self.last_code = code
                self.last_code_was_extracted = str(raw_code or "").strip() != code
                extracted_hint = "，已提取 6 位验证码" if str(raw_code or "").strip() != code else ""
                self.log_fn(
                    f"[号码测试] {entry.phone} 收到验证码 "
                    f"otp={code} otp_received=true otp_length={len(code)}"
                    f" source={self.last_api_parser or 'unknown'}"
                    f"{f'，时间 {code_time}' if code_time else ''}{extracted_hint}"
                )
                return code
            if str(raw_code or "").strip():
                raise RuntimeError("收码 API 返回了验证码字段，但无法提取 6 位数字验证码")

            message = str(poll_result.get("message") or "No verification code").strip()
            status_text = f"{message}{f'，有效期至 {expired_date}' if expired_date else ''}"
            if status_text != last_message:
                self.log_fn(
                    f"[号码测试] {entry.phone} 暂无验证码: {redact_log_text(status_text)} "
                    f"source={self.last_api_parser or 'unknown'}"
                )
                last_message = status_text
            time.sleep(min(float(self.poll_interval_seconds), max(0.1, remaining)))


class SharedPhoneGatewayService:
    """Reuse one local-gateway activation across a batch until it becomes unusable."""

    def __init__(self, base_service, log_fn: Optional[Callable[[str], None]] = None, stop_checker: Optional[Callable[[], None]] = None):
        self.base_service = base_service
        self.log_fn = log_fn or (lambda _msg: None)
        self.stop_checker = stop_checker
        self.current_entry = None
        self._needs_next_sms = False
        self._released = False

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.base_service, "enabled", False))

    @property
    def max_attempts(self) -> int:
        return int(getattr(self.base_service, "max_attempts", 1) or 1)

    @property
    def max_resend_attempts(self) -> int:
        return int(getattr(self.base_service, "max_resend_attempts", 1) or 1)

    @property
    def resend_interval_seconds(self) -> int:
        return int(getattr(self.base_service, "resend_interval_seconds", 0) or 0)

    def prefix_hint(self, phone: str) -> str:
        return self.base_service.prefix_hint(phone)

    def _check_stop(self) -> None:
        if callable(self.stop_checker):
            self.stop_checker()

    def acquire_phone(self, *, exclude_prefixes: Optional[Iterable[str]] = None, **kwargs):
        self._check_stop()
        prefixes = list(exclude_prefixes or [])
        if self.current_entry and not any(str(self.current_entry.phone).startswith(prefix) for prefix in prefixes):
            if self._needs_next_sms:
                if not self.request_next_code(self.current_entry):
                    self.release_current(reason="shared phone cannot request next sms", terminal=True)
                else:
                    self._needs_next_sms = False
            if self.current_entry:
                try:
                    self.base_service.record_reuse(self.current_entry, **kwargs)
                except Exception as exc:
                    self.log_fn(f"[接码网关] 记录批量手机号复用账号失败: {exc}")
                self.log_fn(
                    f"[接码网关] 复用批量手机号: activation_id={self.current_entry.activation_id}, phone={self.current_entry.phone}"
                )
                return self.current_entry

        self.current_entry = self.base_service.acquire_phone(exclude_prefixes=prefixes, **kwargs)
        self._needs_next_sms = False
        self._released = False
        if self.current_entry:
            self.log_fn(
                f"[接码网关] 批量手机号开始复用: activation_id={self.current_entry.activation_id}, phone={self.current_entry.phone}"
            )
        return self.current_entry

    def mark_sms_sent(self, entry) -> None:
        self._check_stop()
        return self.base_service.mark_sms_sent(entry)

    def request_next_code(self, entry) -> bool:
        self._check_stop()
        return self.base_service.request_next_code(entry)

    def wait_for_code(self, entry, *, timeout: Optional[int] = None) -> Optional[str]:
        self._check_stop()
        return self.base_service.wait_for_code(entry, timeout=timeout)

    def complete(self, entry) -> None:
        if self.current_entry and getattr(entry, "activation_id", "") == getattr(self.current_entry, "activation_id", ""):
            self._needs_next_sms = True
            self.log_fn(
                f"[接码网关] 批量手机号保留继续复用: activation_id={entry.activation_id}, phone={mask_phone_for_log(entry.phone)}"
            )
            return None
        return self.base_service.complete(entry)

    def cancel(self, entry, *, reason: str = "") -> None:
        if self.current_entry and getattr(entry, "activation_id", "") == getattr(self.current_entry, "activation_id", ""):
            self.release_current(reason=reason or "shared phone released", terminal=True)
            return None
        return self.base_service.cancel(entry, reason=reason)

    def mark_blacklisted(self, phone: str, *, reason: str = "") -> None:
        if self.current_entry and str(getattr(self.current_entry, "phone", "") or "") == str(phone or "").strip():
            self.release_current(reason=reason or "phone blacklisted by upstream", terminal=True)
            return None
        return self.base_service.mark_blacklisted(phone, reason=reason)

    def release_current(self, *, reason: str = "", terminal: bool = False) -> None:
        if not self.current_entry or self._released:
            self.current_entry = None
            self._needs_next_sms = False
            return
        entry = self.current_entry
        self._released = True
        self.current_entry = None
        self._needs_next_sms = False
        if terminal:
            self.base_service.cancel(entry, reason=reason or "shared phone terminal")
        else:
            self.base_service.complete(entry)


def create_phone_service(config: Optional[dict] = None, log_fn: Optional[Callable[[str], None]] = None):
    cfg = dict(config or {})
    provider = str(cfg.get("chatgpt_phone_verification_provider") or cfg.get("phone_verification_provider") or "smstome").strip().lower()
    if provider in {"local_gateway", "gateway", "local"}:
        return LocalPhoneGatewayService(cfg, log_fn=log_fn)
    return SMSToMePhoneService(cfg, log_fn=log_fn)
