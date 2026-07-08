"""ChatGPT relay 自有手机号池仓储。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests
from sqlmodel import Session, select

from core.db import AccountModel, PhonePoolModel, PhonePrefixStateModel, _ensure_phone_prefix_state_schema, engine

STATUS_ACTIVE = "active"
STATUS_CANNOT_SEND = "cannot_send"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_COOLDOWN = "cooldown"
STATUS_EXHAUSTED = "exhausted"
STATUS_DISABLED = "disabled"

ALL_STATUSES = {
    STATUS_ACTIVE,
    STATUS_CANNOT_SEND,
    STATUS_RATE_LIMITED,
    STATUS_COOLDOWN,
    STATUS_EXHAUSTED,
    STATUS_DISABLED,
}

API_EXPIRY_STATUS_OK = "ok"
API_EXPIRY_STATUS_MISSING = "missing_expired_date"
API_EXPIRY_STATUS_ERROR = "error"
API_EXPIRY_STATUS_SKIPPED = "skipped"

PREFIX_PURPOSE_PHONE_SIGNUP = "phone_signup"
PREFIX_STATUS_AVAILABLE = "available"
PREFIX_STATUS_UNAVAILABLE = "unavailable"
PREFIX_STATUS_UNTESTED = "untested"

_PHONE_SIGNUP_PREFIX_LIMIT_STATUSES = {
    "fraud_guard",
    "phone_restricted_fraud_guard",
    "openai_phone_similar_limit",
}

_PHONE_SIGNUP_PREFIX_LIMIT_MARKERS = (
    "fraud_guard",
    "similar phone numbers",
    "detected suspicious behavior from phone numbers",
    "suspicious behavior from phone numbers",
    "相似号段",
    "相似手机号",
    "相似号码",
)

_PHONE_POOL_API_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.I)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_text() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _extract_host(url: str) -> str:
    try:
        parsed = urlparse((url or "").strip())
        return (parsed.hostname or "").lower()
    except Exception:
        return ""


def normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return ""
    return f"+{digits}"


def _phone_effective_digits(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits


def _phone_prefix4(value: str) -> str:
    digits = _phone_effective_digits(value)
    return digits[:4] if len(digits) >= 4 else ""


def _split_import_phone_api_line(line: str) -> tuple[str, str, list[str]]:
    """Split one operator pasted phone/API row.

    Historical imports use ``手机号----API``.  Operators now also paste
    supplier-native rows such as ``手机号|API``; keep the stored/raw shape
    canonical while accepting both separators.
    """
    text = str(line or "").strip()
    if not text:
        return "", "", []
    if "----" in text:
        parts = text.split("----")
        return parts[0], parts[1] if len(parts) > 1 else "", parts
    if "|" in text:
        phone_part, api_part = text.split("|", 1)
        return phone_part, api_part, [phone_part, api_part]
    if "---" in text:
        parts = text.split("---")
        return parts[0], parts[1] if len(parts) > 1 else "", parts
    match = _PHONE_POOL_API_URL_RE.search(text)
    if not match:
        return "", "", []
    return text[: match.start()], str(match.group(0) or "").rstrip("，,；;"), [
        text[: match.start()],
        str(match.group(0) or "").rstrip("，,；;"),
    ]


def _is_openai_rejected_record(record: "PhonePoolRecord") -> bool:
    if record.status != STATUS_CANNOT_SEND:
        return False
    code = str(record.last_error_code or "").strip().lower()
    message = str(record.last_error_message or "").strip().lower()
    if code == "openai_rejected":
        return True
    return any(
        marker in message
        for marker in (
            "detected suspicious behavior from phone numbers",
            "suspicious behavior from phone numbers",
            "openai 拒绝",
            "手机号无效",
            "号码无效",
            "手机号不支持",
        )
    )


def _parse_import_lines(raw: str) -> tuple[list[tuple[int, str, str, str]], list[dict[str, Any]], list[dict[str, Any]], int]:
    """解析导入文本。

    导入接口只接受“手机号----API”。如果同一批文本里同一手机号出现多次，
    使用最后一次出现的 API/token；额外字段不作为有效期导入，只给 warning。
    """
    entries_by_phone: dict[str, tuple[int, str, str, str]] = {}
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    deduped = 0
    for index, raw_line in enumerate(str(raw or "").splitlines(), start=1):
        line = str(raw_line or "").strip()
        if not line or line.startswith("#"):
            continue
        phone_part, api_part, parts = _split_import_phone_api_line(line)
        if len(parts) < 2:
            errors.append({"line": index, "raw": line, "reason": "缺少手机号/API 分隔符"})
            continue
        phone = normalize_phone(phone_part)
        api_url = str(api_part or "").strip()
        parsed = urlparse(api_url)
        if not phone:
            errors.append({"line": index, "raw": line, "reason": "手机号为空或格式无效"})
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append({"line": index, "raw": line, "phone": phone, "reason": "API URL 无效"})
            continue
        if len(parts) > 2:
            warnings.append({"line": index, "raw": line, "phone": phone, "reason": "导入只使用手机号和 API，已忽略多余字段"})
        if phone in entries_by_phone:
            deduped += 1
            previous_line = entries_by_phone[phone][0]
            warnings.append({
                "line": index,
                "raw": line,
                "phone": phone,
                "reason": f"手机号重复，已使用第 {index} 行覆盖第 {previous_line} 行",
            })
        entries_by_phone[phone] = (index, phone, api_url, line)
    entries = sorted(entries_by_phone.values(), key=lambda item: item[0])
    return entries, errors, warnings, deduped


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _parse_email_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        parsed = []
    if not isinstance(parsed, list):
        return []
    emails: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        email = _normalize_email(item)
        if not email or email in seen:
            continue
        seen.add(email)
        emails.append(email)
    return emails


def _dump_email_list(values: list[str]) -> str:
    emails: list[str] = []
    seen: set[str] = set()
    for value in values:
        email = _normalize_email(value)
        if not email or email in seen:
            continue
        seen.add(email)
        emails.append(email)
    return json.dumps(emails, ensure_ascii=False)


def _extract_expired_date_from_payload(payload: Any) -> str:
    """从收码 API 响应里提取固定有效期。

    当前主协议是 data.expired_date；这里顺手兼容少量常见别名与 expireTime，避免上游字段
    命名略有差异时整批变成 missing。
    """
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates = (
        data.get("expired_date"),
        data.get("api_expired_date"),
        data.get("expires_at"),
        data.get("expire_time"),
        data.get("expireTime"),
        payload.get("expired_date"),
        payload.get("api_expired_date"),
        payload.get("expires_at"),
        payload.get("expire_time"),
        payload.get("expireTime"),
    )
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def probe_phone_api_expiry(api_url: str, *, timeout: float = 12.0) -> dict[str, Any]:
    """一次性探测收码 API 的固定到期时间。"""
    url = str(api_url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {
            "ok": False,
            "status": API_EXPIRY_STATUS_ERROR,
            "expired_date": "",
            "error": "API URL 无效",
        }
    try:
        resp = requests.get(url, timeout=float(timeout or 12.0))
    except Exception as exc:
        return {
            "ok": False,
            "status": API_EXPIRY_STATUS_ERROR,
            "expired_date": "",
            "error": f"请求失败: {exc}",
        }
    try:
        payload = resp.json()
    except Exception:
        return {
            "ok": False,
            "status": API_EXPIRY_STATUS_ERROR,
            "expired_date": "",
            "error": f"响应不是 JSON: {resp.text[:160]}",
        }
    if resp.status_code >= 400:
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("msg") or payload.get("message") or payload.get("error") or "").strip()
        return {
            "ok": False,
            "status": API_EXPIRY_STATUS_ERROR,
            "expired_date": "",
            "error": message or f"HTTP {resp.status_code}",
        }
    expired_date = _extract_expired_date_from_payload(payload)
    if not expired_date:
        return {
            "ok": False,
            "status": API_EXPIRY_STATUS_MISSING,
            "expired_date": "",
            "error": "API 响应未包含 expired_date/expireTime",
        }
    return {
        "ok": True,
        "status": API_EXPIRY_STATUS_OK,
        "expired_date": expired_date,
        "error": "",
    }


@dataclass(slots=True)
class PhonePoolRecord:
    id: int
    phone_e164: str
    api_url: str
    api_host: str
    api_expired_date: str
    api_expiry_checked_at: str
    api_expiry_status: str
    api_expiry_error: str
    label: str
    status: str
    bound_count: int
    bound_account_emails: list[str]
    max_accounts: int
    success_count: int
    fail_count: int
    last_error_code: str
    last_error_message: str
    cooldown_until: str
    last_used_at: str
    created_at: datetime | None
    updated_at: datetime | None

    @property
    def available(self) -> bool:
        if self.status != STATUS_ACTIVE:
            return False
        if self.bound_count >= self.max_accounts:
            return False
        cooldown = _parse_time(self.cooldown_until)
        return not cooldown or cooldown <= _utcnow()

    @property
    def remaining_capacity(self) -> int:
        return max(int(self.max_accounts or 0) - int(self.bound_count or 0), 0)

    def to_dict(self) -> dict[str, Any]:
        def iso(dt: datetime | None) -> str | None:
            return dt.isoformat() if dt else None

        return {
            "id": self.id,
            "phone_e164": self.phone_e164,
            "api_url": self.api_url,
            "api_host": self.api_host,
            "api_expired_date": self.api_expired_date,
            "api_expiry_checked_at": self.api_expiry_checked_at,
            "api_expiry_status": self.api_expiry_status,
            "api_expiry_error": self.api_expiry_error,
            "label": self.label,
            "status": self.status,
            "available": self.available,
            "remaining_capacity": self.remaining_capacity,
            "bound_count": self.bound_count,
            "bound_account_emails": list(self.bound_account_emails),
            "max_accounts": self.max_accounts,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "last_error_code": self.last_error_code,
            "last_error_message": self.last_error_message,
            "cooldown_until": self.cooldown_until or None,
            "last_used_at": self.last_used_at or None,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }


def _to_record(model: PhonePoolModel) -> PhonePoolRecord:
    return PhonePoolRecord(
        id=int(model.id or 0),
        phone_e164=str(model.phone_e164 or ""),
        api_url=str(model.api_url or ""),
        api_host=str(model.api_host or ""),
        api_expired_date=str(getattr(model, "api_expired_date", "") or ""),
        api_expiry_checked_at=str(getattr(model, "api_expiry_checked_at", "") or ""),
        api_expiry_status=str(getattr(model, "api_expiry_status", "") or ""),
        api_expiry_error=str(getattr(model, "api_expiry_error", "") or ""),
        label=str(model.label or ""),
        status=str(model.status or STATUS_ACTIVE),
        bound_count=int(model.bound_count or 0),
        bound_account_emails=_parse_email_list(getattr(model, "bound_account_emails_json", "[]")),
        max_accounts=max(int(model.max_accounts or 3), 1),
        success_count=int(model.success_count or 0),
        fail_count=int(model.fail_count or 0),
        last_error_code=str(model.last_error_code or ""),
        last_error_message=str(model.last_error_message or ""),
        cooldown_until=str(model.cooldown_until or ""),
        last_used_at=str(model.last_used_at or ""),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _is_prefix_sample_candidate_record(record: PhonePoolRecord) -> bool:
    if record.status in {STATUS_DISABLED, STATUS_EXHAUSTED}:
        return False
    if not record.api_url:
        return False
    if not _phone_prefix4(record.phone_e164):
        return False
    if record.remaining_capacity <= 0:
        return False
    return True


def _rejected_prefix_set(records: list[PhonePoolRecord]) -> set[str]:
    prefixes: set[str] = set()
    for record in records:
        if not _is_openai_rejected_record(record):
            continue
        prefix = _phone_prefix4(record.phone_e164)
        if prefix:
            prefixes.add(prefix)
    return prefixes


def _phone_signup_prefix_status_from_counts(success_count: int, failure_count: int) -> str:
    # 注册号段仍然“成功优先”：同段有成功样本就视为可用。
    # 这里的 failure_count 只记录相似号段被限制（fraud_guard）这种强号段信号，
    # 不把已占用、收码失败、代理/CSRF 等单号/环境失败污染成号段不可用。
    if int(success_count or 0) > 0:
        return PREFIX_STATUS_AVAILABLE
    if int(failure_count or 0) > 0:
        return PREFIX_STATUS_UNAVAILABLE
    return PREFIX_STATUS_UNTESTED


def is_phone_signup_prefix_unavailable_signal(task_status: str, reason: str = "") -> bool:
    status = str(task_status or "").strip().lower()
    if status in _PHONE_SIGNUP_PREFIX_LIMIT_STATUSES:
        return True
    text = f"{status} {reason or ''}".lower()
    return any(marker in text for marker in _PHONE_SIGNUP_PREFIX_LIMIT_MARKERS)


def _phone_signup_prefix_signal(task_status: str, reason: str = "") -> str:
    status = str(task_status or "").strip()
    if status in {"registered_phone_signup"}:
        return "success"
    if is_phone_signup_prefix_unavailable_signal(status, reason):
        return "failure"
    return ""


def _prefix_state_to_item(model: PhonePrefixStateModel) -> dict[str, Any]:
    success_count = int(getattr(model, "success_count", 0) or 0)
    failure_count = int(getattr(model, "failure_count", 0) or 0)
    return {
        "prefix": str(getattr(model, "prefix", "") or ""),
        "purpose": str(getattr(model, "purpose", "") or ""),
        "status": str(getattr(model, "status", "") or ""),
        "success_count": success_count,
        "failure_count": failure_count,
        "count": success_count + failure_count,
        "total": success_count + failure_count,
        "last_success_phone": str(getattr(model, "last_success_phone", "") or ""),
        "last_failure_phone": str(getattr(model, "last_failure_phone", "") or ""),
        "last_error_code": str(getattr(model, "last_error_code", "") or ""),
        "last_error_message": str(getattr(model, "last_error_message", "") or ""),
        "last_seen_at": str(getattr(model, "last_seen_at", "") or ""),
    }


def _build_phone_signup_prefix_health() -> dict[str, list[dict[str, Any]]]:
    try:
        _ensure_phone_prefix_state_schema()
    except Exception:
        return {PREFIX_STATUS_AVAILABLE: [], PREFIX_STATUS_UNAVAILABLE: [], PREFIX_STATUS_UNTESTED: []}
    with Session(engine) as session:
        rows = session.exec(
            select(PhonePrefixStateModel)
            .where(PhonePrefixStateModel.purpose == PREFIX_PURPOSE_PHONE_SIGNUP)
            .order_by(PhonePrefixStateModel.prefix)
        ).all()
    groups: dict[str, list[dict[str, Any]]] = {
        PREFIX_STATUS_AVAILABLE: [],
        PREFIX_STATUS_UNAVAILABLE: [],
        PREFIX_STATUS_UNTESTED: [],
    }
    for row in rows:
        item = _prefix_state_to_item(row)
        status = str(item.get("status") or PREFIX_STATUS_UNTESTED)
        groups.setdefault(status, []).append(item)

    def sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        status = str(item.get("status") or "")
        primary = int(item.get("success_count") or 0) if status == PREFIX_STATUS_AVAILABLE else int(item.get("failure_count") or 0)
        return (-primary, -int(item.get("total") or 0), str(item.get("prefix") or ""))

    for values in groups.values():
        values.sort(key=sort_key)
    return groups


def _build_prefix_health(records: list[PhonePoolRecord]) -> dict[str, list[dict[str, Any]]]:
    stats_by_prefix: dict[str, dict[str, Any]] = {}
    for record in records:
        prefix = _phone_prefix4(record.phone_e164)
        if not prefix:
            continue
        stats = stats_by_prefix.setdefault(
            prefix,
            {
                "prefix": prefix,
                "total": 0,
                "available_count": 0,
                "remaining_capacity": 0,
                "rejected_count": 0,
                "bind_limit_count": 0,
                "active_count": 0,
                "cannot_send_count": 0,
                "rate_limited_count": 0,
                "cooldown_count": 0,
                "exhausted_count": 0,
                "disabled_count": 0,
            },
        )
        status = str(record.status or STATUS_ACTIVE)
        stats["total"] += 1
        stats[f"{status}_count"] = int(stats.get(f"{status}_count", 0)) + 1
        if record.available:
            stats["available_count"] += 1
            stats["remaining_capacity"] += int(record.remaining_capacity or 0)
        if _is_openai_rejected_record(record):
            stats["rejected_count"] += 1
        if status == STATUS_EXHAUSTED or int(record.remaining_capacity or 0) <= 0:
            stats["bind_limit_count"] += 1

    groups: dict[str, list[dict[str, Any]]] = {
        "available": [],
        "unavailable": [],
        "exhausted": [],
        "temporary": [],
    }
    for stats in stats_by_prefix.values():
        rejected_count = int(stats.get("rejected_count") or 0)
        available_count = int(stats.get("available_count") or 0)
        total = int(stats.get("total") or 0)
        bind_limit_count = int(stats.get("bind_limit_count") or 0)
        if available_count > 0:
            status = "available"
        elif rejected_count > 0:
            status = "unavailable"
        elif total > 0 and bind_limit_count >= total:
            status = "exhausted"
        else:
            status = "temporary"
        item = {**stats, "status": status}
        groups[status].append(item)

    def sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        if item.get("status") == "unavailable":
            primary = int(item.get("rejected_count") or 0)
        elif item.get("status") == "available":
            primary = int(item.get("remaining_capacity") or 0)
        elif item.get("status") == "exhausted":
            primary = int(item.get("bind_limit_count") or 0)
        else:
            primary = int(item.get("total") or 0)
        return (-primary, -int(item.get("total") or 0), str(item.get("prefix") or ""))

    for items in groups.values():
        items.sort(key=sort_key)
    return groups




def _self_unavailable_reason(record: PhonePoolRecord) -> str:
    """返回号码自身不可用原因，不把号段策略混进来。"""
    status = str(record.status or STATUS_ACTIVE)
    cooldown = _parse_time(record.cooldown_until)
    if status == STATUS_DISABLED:
        return "disabled"
    if status == STATUS_CANNOT_SEND:
        code = str(record.last_error_code or "").strip().lower()
        if code == "openai_rejected":
            return "openai_rejected"
        if code in {"api_no_code", "api_error"}:
            return code
        return "cannot_send"
    if status == STATUS_RATE_LIMITED:
        return "rate_limited"
    if status == STATUS_COOLDOWN or (cooldown and cooldown > _utcnow()):
        return "cooldown"
    if status == STATUS_EXHAUSTED:
        code = str(record.last_error_code or "").strip().lower()
        if code == "phone_already_used":
            return "phone_already_used"
        return "exhausted"
    if int(record.remaining_capacity or 0) <= 0:
        return "no_capacity"
    if not str(record.api_url or "").strip():
        return "missing_api_url"
    if status != STATUS_ACTIVE:
        return status or "unknown"
    if not record.available:
        return "self_unavailable"
    return ""


def _build_prefix_health_map(records: list[PhonePoolRecord]) -> dict[str, dict[str, Any]]:
    groups = _build_prefix_health(records)
    mapping: dict[str, dict[str, Any]] = {}
    for items in groups.values():
        for item in items:
            prefix = str(item.get("prefix") or "")
            if prefix:
                mapping[prefix] = item
    return mapping


def _ordinary_task_block_reason(record: PhonePoolRecord, prefix_item: dict[str, Any] | None) -> str:
    self_reason = _self_unavailable_reason(record)
    if self_reason:
        return self_reason
    prefix_status = str((prefix_item or {}).get("status") or "unknown")
    if prefix_status == "available":
        return ""
    if prefix_status == "unavailable":
        return "prefix_unavailable"
    if prefix_status == "exhausted":
        return "prefix_exhausted"
    if prefix_status == "temporary":
        return "prefix_temporary"
    return "prefix_unknown"


def serialize_phone_pool_records(
    records: list[PhonePoolRecord],
    *,
    all_records: list[PhonePoolRecord] | None = None,
) -> list[dict[str, Any]]:
    """序列化手机号池行，并附加派生状态。

    status/available 继续保持号码自身旧语义；新增字段把“号段健康”和
    “普通任务最终是否会选”表达清楚，避免用号段策略污染号码自身状态。
    """
    universe = list(all_records) if all_records is not None else list(records)
    prefix_map = _build_prefix_health_map(universe)
    items: list[dict[str, Any]] = []
    for record in records:
        prefix = _phone_prefix4(record.phone_e164)
        prefix_item = prefix_map.get(prefix) if prefix else None
        self_reason = _self_unavailable_reason(record)
        self_available = not self_reason
        block_reason = _ordinary_task_block_reason(record, prefix_item)
        prefix_status = str((prefix_item or {}).get("status") or "unknown")
        item = record.to_dict()
        item.update(
            {
                "prefix": prefix,
                "self_available": self_available,
                "self_unavailable_reason": self_reason,
                "prefix_status": prefix_status,
                "prefix_total": int((prefix_item or {}).get("total") or 0),
                "prefix_available_count": int((prefix_item or {}).get("available_count") or 0),
                "prefix_rejected_count": int((prefix_item or {}).get("rejected_count") or 0),
                "prefix_remaining_capacity": int((prefix_item or {}).get("remaining_capacity") or 0),
                "ordinary_task_eligible": not block_reason,
                "ordinary_task_block_reason": block_reason,
            }
        )
        items.append(item)
    return items


class PhonePoolRepository:
    @staticmethod
    def summarize(records: list[PhonePoolRecord]) -> dict[str, Any]:
        by_status = {status: 0 for status in ALL_STATUSES}
        prefix_health = _build_prefix_health(records)
        available_prefixes = list(prefix_health["available"])
        unavailable_prefixes = list(prefix_health["unavailable"])
        exhausted_prefixes = list(prefix_health["exhausted"])
        temporary_prefixes = list(prefix_health["temporary"])
        rejected_prefix_counts = {
            str(item.get("prefix") or ""): int(item.get("rejected_count") or 0)
            for item in unavailable_prefixes
            if str(item.get("prefix") or "")
        }
        prefix_sample_candidate_counts: dict[str, int] = {}
        for record in records:
            status = str(record.status or STATUS_ACTIVE)
            by_status[status] = by_status.get(status, 0) + 1
            if _is_prefix_sample_candidate_record(record):
                prefix = _phone_prefix4(record.phone_e164)
                if prefix:
                    prefix_sample_candidate_counts[prefix] = prefix_sample_candidate_counts.get(prefix, 0) + 1
        available = sum(int(item.get("available_count") or 0) for item in available_prefixes)
        remaining_capacity = sum(int(item.get("remaining_capacity") or 0) for item in available_prefixes)
        number_available = sum(1 for record in records if record.available)
        number_remaining_capacity = sum(int(record.remaining_capacity or 0) for record in records if record.available)
        rejected_prefixes = [
            {
                **item,
                "count": int(item.get("rejected_count") or 0),
            }
            for item in unavailable_prefixes
        ]
        phone_signup_prefix_health = _build_phone_signup_prefix_health()
        phone_signup_available_prefixes = list(phone_signup_prefix_health.get(PREFIX_STATUS_AVAILABLE) or [])
        phone_signup_unavailable_prefixes = list(phone_signup_prefix_health.get(PREFIX_STATUS_UNAVAILABLE) or [])
        phone_signup_untested_prefixes = list(phone_signup_prefix_health.get(PREFIX_STATUS_UNTESTED) or [])
        return {
            "total": len(records),
            "available": available,
            "remaining_capacity": remaining_capacity,
            "number_available": number_available,
            "number_remaining_capacity": number_remaining_capacity,
            "rate_limited": by_status.get(STATUS_RATE_LIMITED, 0),
            "unavailable": by_status.get(STATUS_CANNOT_SEND, 0),
            "cannot_send": by_status.get(STATUS_CANNOT_SEND, 0),
            "cooldown": by_status.get(STATUS_COOLDOWN, 0),
            "exhausted": by_status.get(STATUS_EXHAUSTED, 0),
            "disabled": by_status.get(STATUS_DISABLED, 0),
            "active": by_status.get(STATUS_ACTIVE, 0),
            "available_prefix_count": len(available_prefixes),
            "available_prefixes": available_prefixes,
            "available_prefix_sample_1": sum(min(int(item.get("available_count") or 0), 1) for item in available_prefixes),
            "available_prefix_sample_2": sum(min(int(item.get("available_count") or 0), 2) for item in available_prefixes),
            "prefix_sample_prefix_count": len(prefix_sample_candidate_counts),
            "prefix_sample_phone_count": sum(prefix_sample_candidate_counts.values()),
            "prefix_sample_count_1": sum(min(count, 1) for count in prefix_sample_candidate_counts.values()),
            "prefix_sample_count_2": sum(min(count, 2) for count in prefix_sample_candidate_counts.values()),
            "rejected_phone_count": sum(rejected_prefix_counts.values()),
            "rejected_prefix_count": len(rejected_prefixes),
            "rejected_prefix_sample_1": sum(min(count, 1) for count in rejected_prefix_counts.values()),
            "rejected_prefix_sample_2": sum(min(count, 2) for count in rejected_prefix_counts.values()),
            "rejected_prefixes": rejected_prefixes,
            "exhausted_prefix_count": len(exhausted_prefixes),
            "exhausted_prefixes": exhausted_prefixes,
            "temporary_prefix_count": len(temporary_prefixes),
            "temporary_prefixes": temporary_prefixes,
            "prefix_health": {
                "available": available_prefixes,
                "unavailable": unavailable_prefixes,
                "exhausted": exhausted_prefixes,
                "temporary": temporary_prefixes,
            },
            "phone_signup_available_prefix_count": len(phone_signup_available_prefixes),
            "phone_signup_available_prefixes": phone_signup_available_prefixes,
            "phone_signup_unavailable_prefix_count": len(phone_signup_unavailable_prefixes),
            "phone_signup_unavailable_prefixes": phone_signup_unavailable_prefixes,
            "phone_signup_untested_prefix_count": len(phone_signup_untested_prefixes),
            "phone_signup_prefix_health": {
                "available": phone_signup_available_prefixes,
                "unavailable": phone_signup_unavailable_prefixes,
                "untested": phone_signup_untested_prefixes,
            },
        }

    def list(self, *, status: str = "") -> list[PhonePoolRecord]:
        with Session(engine) as session:
            stmt = select(PhonePoolModel)
            if status:
                stmt = stmt.where(PhonePoolModel.status == status)
            stmt = stmt.order_by(PhonePoolModel.id)
            items = session.exec(stmt).all()
        return [_to_record(item) for item in items]

    def get(self, phone: str) -> PhonePoolRecord | None:
        phone_norm = normalize_phone(phone)
        if not phone_norm:
            return None
        with Session(engine) as session:
            model = session.exec(
                select(PhonePoolModel).where(PhonePoolModel.phone_e164 == phone_norm)
            ).first()
        return _to_record(model) if model else None

    def add(self, *, phone: str, api_url: str, label: str = "", max_accounts: int = 3, api_expired_date: str = "") -> PhonePoolRecord | None:
        phone_norm = normalize_phone(phone)
        api_url = str(api_url or "").strip()
        parsed = urlparse(api_url)
        api_expired_date = str(api_expired_date or "").strip()
        if not phone_norm or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        now = _utcnow()
        with Session(engine) as session:
            existing = session.exec(
                select(PhonePoolModel).where(PhonePoolModel.phone_e164 == phone_norm)
            ).first()
            if existing is None:
                model = PhonePoolModel(
                    phone_e164=phone_norm,
                    api_url=api_url,
                    api_host=_extract_host(api_url),
                    api_expired_date=api_expired_date,
                    api_expiry_checked_at=_now_text() if api_expired_date else "",
                    api_expiry_status=API_EXPIRY_STATUS_OK if api_expired_date else "",
                    api_expiry_error="",
                    label=str(label or "").strip(),
                    max_accounts=max(int(max_accounts or 3), 1),
                    created_at=now,
                    updated_at=now,
                )
                session.add(model)
                session.commit()
                session.refresh(model)
                return _to_record(model)
            api_changed = str(existing.api_url or "").strip() != api_url
            existing.api_url = api_url
            existing.api_host = _extract_host(api_url)
            if api_expired_date:
                existing.api_expired_date = api_expired_date
                existing.api_expiry_checked_at = _now_text()
                existing.api_expiry_status = API_EXPIRY_STATUS_OK
                existing.api_expiry_error = ""
            elif api_changed:
                existing.api_expired_date = ""
                existing.api_expiry_checked_at = ""
                existing.api_expiry_status = ""
                existing.api_expiry_error = ""
            if label:
                existing.label = str(label or "").strip()
            if max_accounts:
                existing.max_accounts = max(int(max_accounts or 3), 1)
            existing.updated_at = now
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return _to_record(existing)

    def import_lines(self, raw: str) -> dict[str, Any]:
        entries, parse_errors, warnings, deduped = _parse_import_lines(raw)
        added = updated = unchanged = skipped = api_replaced = 0
        imported_ids: list[int] = []
        refresh_ids: list[int] = []
        seen_refresh_ids: set[int] = set()
        for _line_no, phone, api_url, _raw_line in entries:
            before = self.get(phone)
            if before is not None and str(before.api_url or "").strip() == api_url:
                record = before
                unchanged += 1
            else:
                record = self.add(phone=phone, api_url=api_url)
                if record is None:
                    skipped += 1
                elif before is None:
                    added += 1
                    imported_ids.append(int(record.id or 0))
                else:
                    updated += 1
                    api_replaced += 1
                    imported_ids.append(int(record.id or 0))
            if (
                record is not None
                and int(record.id or 0) > 0
                and not str(record.api_expired_date or "").strip()
                and not str(record.api_expiry_checked_at or "").strip()
                and int(record.id or 0) not in seen_refresh_ids
            ):
                seen_refresh_ids.add(int(record.id or 0))
                refresh_ids.append(int(record.id or 0))
        return {
            "added": added,
            "updated": updated,
            "unchanged": unchanged,
            "skipped": skipped,
            "deduped": deduped,
            "api_replaced": api_replaced,
            "errors": parse_errors,
            "warnings": warnings,
            "ids": [item_id for item_id in imported_ids if item_id > 0],
            "refresh_ids": [item_id for item_id in refresh_ids if item_id > 0],
        }

    def update(self, record_id: int, *, api_url: str | None = None, label: str | None = None, max_accounts: int | None = None, status: str | None = None, api_expired_date: str | None = None) -> PhonePoolRecord | None:
        with Session(engine) as session:
            model = session.get(PhonePoolModel, int(record_id))
            if model is None:
                return None
            if api_url is not None:
                cleaned = str(api_url or "").strip()
                parsed = urlparse(cleaned)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    return None
                model.api_url = cleaned
                model.api_host = _extract_host(cleaned)
            if label is not None:
                model.label = str(label or "").strip()
            if api_expired_date is not None:
                model.api_expired_date = str(api_expired_date or "").strip()
                model.api_expiry_checked_at = _now_text() if model.api_expired_date else ""
                model.api_expiry_status = API_EXPIRY_STATUS_OK if model.api_expired_date else ""
                model.api_expiry_error = ""
            if max_accounts is not None:
                model.max_accounts = max(int(max_accounts or 3), 1)
            if status is not None and status in ALL_STATUSES:
                model.status = status
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def set_enabled(self, record_id: int, enabled: bool) -> PhonePoolRecord | None:
        with Session(engine) as session:
            model = session.get(PhonePoolModel, int(record_id))
            if model is None:
                return None
            model.status = STATUS_ACTIVE if enabled else STATUS_DISABLED
            if enabled:
                model.cooldown_until = ""
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def reset_status(self, record_id: int) -> PhonePoolRecord | None:
        with Session(engine) as session:
            model = session.get(PhonePoolModel, int(record_id))
            if model is None:
                return None
            model.status = STATUS_ACTIVE
            model.cooldown_until = ""
            model.last_error_code = ""
            model.last_error_message = ""
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def delete(self, record_id: int) -> bool:
        with Session(engine) as session:
            model = session.get(PhonePoolModel, int(record_id))
            if model is None:
                return False
            session.delete(model)
            session.commit()
            return True

    def list_available(self) -> list[PhonePoolRecord]:
        """返回当前普通绑定可用号码。

        号段恢复规则改为“同号段只要有一个自身可用号码，整段恢复可用”，
        所以普通绑定不再因为同 prefix 有历史 rejected 样本而跳过整段。
        """
        now = _utcnow()
        with Session(engine) as session:
            candidates = session.exec(
                select(PhonePoolModel)
                .where(PhonePoolModel.status == STATUS_ACTIVE)
                .where(PhonePoolModel.bound_count < PhonePoolModel.max_accounts)
                .order_by(PhonePoolModel.last_used_at, PhonePoolModel.id)
            ).all()
        items: list[PhonePoolRecord] = []
        for model in candidates:
            rec = _to_record(model)
            if not rec.api_url:
                continue
            cooldown = _parse_time(rec.cooldown_until)
            if cooldown and cooldown > now:
                continue
            if rec.remaining_capacity <= 0:
                continue
            items.append(rec)
        return items

    def list_available_by_prefixes(self, prefixes: list[str]) -> list[PhonePoolRecord]:
        """返回指定号段内号码自身可用的绑定候选。

        限定号段绑定表达的是“用户明确要这个号段”，所以不再用整段健康
        状态拦截。即使某个号段因为历史 OpenAI 拒绝被归到不可用号段，只要
        该号段内仍有自身 active、未冷却、未绑满且带 API 的号码，就允许
        本次任务使用。
        """
        selected = {
            "".join(ch for ch in str(prefix or "") if ch.isdigit())[:4]
            for prefix in prefixes or []
        }
        selected = {prefix for prefix in selected if len(prefix) == 4}
        if not selected:
            return []
        now = _utcnow()
        with Session(engine) as session:
            candidates = session.exec(
                select(PhonePoolModel)
                .where(PhonePoolModel.status == STATUS_ACTIVE)
                .where(PhonePoolModel.bound_count < PhonePoolModel.max_accounts)
                .order_by(PhonePoolModel.last_used_at, PhonePoolModel.id)
            ).all()
        items: list[PhonePoolRecord] = []
        for model in candidates:
            rec = _to_record(model)
            if _phone_prefix4(rec.phone_e164) not in selected:
                continue
            if not rec.api_url:
                continue
            cooldown = _parse_time(rec.cooldown_until)
            if cooldown and cooldown > now:
                continue
            if rec.remaining_capacity <= 0:
                continue
            items.append(rec)
        return items

    def pick_available(self, account_count: int) -> list[PhonePoolRecord]:
        """兼容旧调用：按剩余容量取足够覆盖 account_count 的号码记录。"""
        need = max(int(account_count or 0), 0)
        if need <= 0:
            return []
        picked: list[PhonePoolRecord] = []
        slots = 0
        for rec in self.list_available():
            picked.append(rec)
            slots += rec.remaining_capacity
            if slots >= need:
                break
        return picked

    def list_prefix_sample_candidates(self) -> list[PhonePoolRecord]:
        """返回全部号段抽样候选：不按号段健康状态过滤。"""
        return [
            record
            for record in self.list()
            if _is_prefix_sample_candidate_record(record)
        ]

    def sample_testable_by_prefix(self, sample_size: int = 1) -> list[PhonePoolRecord]:
        """按手机号前 4 位抽样，优先覆盖全部号段，再补每段第 2 个号码。"""
        return self._sample_records_by_prefix(self.list_prefix_sample_candidates(), sample_size=sample_size)

    def sample_available_by_prefix(self, sample_size: int = 1) -> list[PhonePoolRecord]:
        """按手机号前 4 位抽样，只测试健康可用号段。"""
        return self._sample_records_by_prefix(self.list_available(), sample_size=sample_size)

    def sample_rejected_by_prefix(self, sample_size: int = 1) -> list[PhonePoolRecord]:
        """只从 OpenAI 明确拒绝过的号段/号码中抽样，用于复测不可用号段。"""
        return self._sample_records_by_prefix(
            [record for record in self.list() if _is_prefix_sample_candidate_record(record) and _is_openai_rejected_record(record)],
            sample_size=sample_size,
        )

    def sample_selected_prefixes(self, prefixes: list[str], sample_size: int = 1) -> list[PhonePoolRecord]:
        """按人工指定号段抽样，不再按可用/不可用状态过滤号段。"""
        selected = {
            "".join(ch for ch in str(prefix or "") if ch.isdigit())[:4]
            for prefix in prefixes or []
        }
        selected.discard("")
        if not selected:
            return []
        return self._sample_records_by_prefix(
            [
                record
                for record in self.list()
                if _is_prefix_sample_candidate_record(record)
                and _phone_prefix4(record.phone_e164) in selected
            ],
            sample_size=sample_size,
        )

    def _sample_records_by_prefix(self, records: list[PhonePoolRecord], *, sample_size: int = 1) -> list[PhonePoolRecord]:
        """按手机号前 4 位抽样，优先覆盖每个号段，再补每段第 2 个号码。"""
        size = int(sample_size or 1)
        if size not in {1, 2}:
            raise ValueError("sample_size must be 1 or 2")

        grouped: dict[str, list[PhonePoolRecord]] = {}
        for record in records:
            prefix = _phone_prefix4(record.phone_e164)
            if not prefix:
                continue
            grouped.setdefault(prefix, []).append(record)

        for records in grouped.values():
            records.sort(
                key=lambda record: (
                    int(record.success_count or 0) + int(record.fail_count or 0),
                    0 if not str(record.last_used_at or "").strip() else 1,
                    str(record.last_used_at or ""),
                    int(record.id or 0),
                )
            )

        selected: list[PhonePoolRecord] = []
        prefixes = sorted(grouped)
        for sample_index in range(size):
            for prefix in prefixes:
                records = grouped[prefix]
                if sample_index < len(records):
                    selected.append(records[sample_index])
        return selected

    def restore_prefix_sample_records(self, record_ids: list[int]) -> list[PhonePoolRecord]:
        """恢复本次号段抽样选中的号码，让旧失败/限流号码可重新进入测试。"""
        ids = [int(value or 0) for value in record_ids if int(value or 0) > 0]
        if not ids:
            return []
        seen: set[int] = set()
        ordered_ids: list[int] = []
        for record_id in ids:
            if record_id in seen:
                continue
            seen.add(record_id)
            ordered_ids.append(record_id)

        now = _utcnow()
        restored_ids: set[int] = set()
        restored_by_id: dict[int, PhonePoolRecord] = {}
        with Session(engine) as session:
            for record_id in ordered_ids:
                model = session.get(PhonePoolModel, record_id)
                if model is None:
                    continue
                record = _to_record(model)
                if not _is_prefix_sample_candidate_record(record):
                    continue
                model.status = STATUS_ACTIVE
                model.cooldown_until = ""
                model.last_error_code = ""
                model.last_error_message = ""
                model.updated_at = now
                session.add(model)
                restored_ids.add(record_id)
            session.commit()
            for record_id in ordered_ids:
                if record_id not in restored_ids:
                    continue
                model = session.get(PhonePoolModel, record_id)
                if model is not None:
                    restored_by_id[record_id] = _to_record(model)
        return [restored_by_id[record_id] for record_id in ordered_ids if record_id in restored_by_id]

    def to_phone_items(
        self,
        records: list[PhonePoolRecord],
        *,
        limit_accounts: int = 0,
        expand_capacity: bool = False,
    ) -> list[dict[str, Any]]:
        """把号池记录转换为 phone-binding-test 的 phone_items。

        默认每个手机号只生成一个任务项，避免某个号失败后因为剩余容量 slot
        被重复尝试。只有显式开启同号连续绑定时才按剩余容量展开。
        """
        items: list[dict[str, Any]] = []
        max_items = max(int(limit_accounts or 0), 0)
        line_no = 0
        for record in records:
            repeat = record.remaining_capacity if expand_capacity else 1
            for _ in range(max(int(repeat or 0), 1)):
                if max_items and len(items) >= max_items:
                    return items
                line_no += 1
                raw_line = f"{record.phone_e164}----{record.api_url}"
                if record.api_expired_date:
                    raw_line = f"{raw_line}----{record.api_expired_date}"
                items.append({
                    "id": record.id,
                    "pool_id": record.id,
                    "line_no": line_no,
                    "phone": record.phone_e164,
                    "api_url": record.api_url,
                    "api_expired_date": record.api_expired_date,
                    "raw_line": raw_line,
                    "pool_managed": True,
                    "prefix4": _phone_prefix4(record.phone_e164),
                })
        return items

    def update_api_expired_date(self, phone: str, api_expired_date: str) -> PhonePoolRecord | None:
        phone_norm = normalize_phone(phone)
        expired = str(api_expired_date or "").strip()
        if not phone_norm or not expired:
            return self.get(phone_norm) if phone_norm else None
        with Session(engine) as session:
            model = session.exec(
                select(PhonePoolModel).where(PhonePoolModel.phone_e164 == phone_norm)
            ).first()
            if model is None:
                return None
            model.api_expired_date = expired
            model.api_expiry_checked_at = _now_text()
            model.api_expiry_status = API_EXPIRY_STATUS_OK
            model.api_expiry_error = ""
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def refresh_api_expiry_for_ids(
        self,
        record_ids: list[int],
        *,
        force: bool = False,
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        """一次性补全指定手机号的 API 到期时间。

        默认只处理 api_expired_date 为空的记录；有效期是固定值，已有值不重复请求。
        """
        ordered_ids: list[int] = []
        seen: set[int] = set()
        for value in record_ids or []:
            try:
                record_id = int(value)
            except (TypeError, ValueError):
                continue
            if record_id <= 0 or record_id in seen:
                continue
            seen.add(record_id)
            ordered_ids.append(record_id)

        results: list[dict[str, Any]] = []
        summary = {
            "total": len(ordered_ids),
            "checked": 0,
            "success": 0,
            "missing_expired_date": 0,
            "error": 0,
            "skipped": 0,
            "not_found": 0,
        }
        for record_id in ordered_ids:
            with Session(engine) as session:
                model = session.get(PhonePoolModel, int(record_id))
                if model is None:
                    summary["not_found"] += 1
                    results.append({"id": record_id, "status": "not_found", "error": "号码不存在"})
                    continue
                record = _to_record(model)

            if not force and str(record.api_expired_date or "").strip():
                summary["skipped"] += 1
                results.append({"id": record_id, "status": API_EXPIRY_STATUS_SKIPPED, "item": record.to_dict()})
                continue
            if not str(record.api_url or "").strip():
                checked_at = _now_text()
                with Session(engine) as session:
                    model = session.get(PhonePoolModel, int(record_id))
                    if model is not None:
                        model.api_expiry_checked_at = checked_at
                        model.api_expiry_status = API_EXPIRY_STATUS_ERROR
                        model.api_expiry_error = "API URL 为空"
                        model.updated_at = _utcnow()
                        session.add(model)
                        session.commit()
                        session.refresh(model)
                        record = _to_record(model)
                summary["checked"] += 1
                summary["error"] += 1
                results.append({"id": record_id, "status": API_EXPIRY_STATUS_ERROR, "error": "API URL 为空", "item": record.to_dict()})
                continue

            probe = probe_phone_api_expiry(record.api_url, timeout=timeout)
            checked_at = _now_text()
            with Session(engine) as session:
                model = session.get(PhonePoolModel, int(record_id))
                if model is None:
                    summary["not_found"] += 1
                    results.append({"id": record_id, "status": "not_found", "error": "号码不存在"})
                    continue
                status = str(probe.get("status") or API_EXPIRY_STATUS_ERROR)
                expired = str(probe.get("expired_date") or "").strip()
                model.api_expiry_checked_at = checked_at
                model.api_expiry_status = status
                model.api_expiry_error = str(probe.get("error") or "")[:500]
                if expired:
                    model.api_expired_date = expired
                model.updated_at = _utcnow()
                session.add(model)
                session.commit()
                session.refresh(model)
                record = _to_record(model)

            summary["checked"] += 1
            if status == API_EXPIRY_STATUS_OK:
                summary["success"] += 1
            elif status == API_EXPIRY_STATUS_MISSING:
                summary["missing_expired_date"] += 1
            else:
                summary["error"] += 1
            results.append({
                "id": record_id,
                "status": status,
                "expired_date": str(probe.get("expired_date") or ""),
                "error": str(probe.get("error") or ""),
                "item": record.to_dict(),
            })
        return {"ok": True, "summary": summary, "results": results}

    def refresh_missing_api_expiry(
        self,
        *,
        limit: int = 50,
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        """后台空闲补一次历史空值。"""
        max_items = max(min(int(limit or 50), 200), 1)
        with Session(engine) as session:
            rows = session.exec(
                select(PhonePoolModel)
                .where(PhonePoolModel.api_url != "")
                .where(PhonePoolModel.api_expired_date == "")
                .where(PhonePoolModel.api_expiry_checked_at == "")
                .order_by(PhonePoolModel.id)
                .limit(max_items)
            ).all()
            ids = [int(row.id or 0) for row in rows if int(row.id or 0) > 0]
        return self.refresh_api_expiry_for_ids(ids, force=False, timeout=timeout)

    def record_success(self, phone: str, *, email: str = "") -> PhonePoolRecord | None:
        phone_norm = normalize_phone(phone)
        now = _utcnow()
        email_norm = _normalize_email(email)
        with Session(engine) as session:
            model = session.exec(
                select(PhonePoolModel).where(PhonePoolModel.phone_e164 == phone_norm)
            ).first()
            if model is None:
                return None
            bound_emails = _parse_email_list(getattr(model, "bound_account_emails_json", "[]"))
            if email_norm:
                if email_norm not in bound_emails:
                    bound_emails.append(email_norm)
                    model.bound_count = int(model.bound_count or 0) + 1
                model.bound_account_emails_json = _dump_email_list(bound_emails)
            else:
                model.bound_count = int(model.bound_count or 0) + 1
            model.success_count = int(model.success_count or 0) + 1
            model.last_used_at = _now_text()
            model.last_error_code = ""
            model.last_error_message = ""
            model.cooldown_until = ""
            if int(model.bound_count or 0) >= int(model.max_accounts or 3):
                model.status = STATUS_EXHAUSTED
            else:
                model.status = STATUS_ACTIVE
            model.updated_at = now
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def record_phone_signup_success(self, phone: str, *, email: str = "") -> PhonePoolRecord | None:
        """手机号注册成功：手机号本身就是账号标识，后续不应再被普通任务复用。"""
        phone_norm = normalize_phone(phone)
        now = _utcnow()
        email_norm = _normalize_email(email)
        with Session(engine) as session:
            model = session.exec(
                select(PhonePoolModel).where(PhonePoolModel.phone_e164 == phone_norm)
            ).first()
            if model is None:
                return None
            bound_emails = _parse_email_list(getattr(model, "bound_account_emails_json", "[]"))
            if email_norm and email_norm not in bound_emails:
                bound_emails.append(email_norm)
            model.bound_account_emails_json = _dump_email_list(bound_emails)
            model.bound_count = max(int(model.bound_count or 0) + 1, int(model.max_accounts or 1))
            model.success_count = int(model.success_count or 0) + 1
            model.last_used_at = _now_text()
            model.last_error_code = "registered_phone_signup"
            model.last_error_message = ""
            model.cooldown_until = ""
            model.status = STATUS_EXHAUSTED
            model.updated_at = now
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def record_failure(self, phone: str, *, status: str, error_code: str = "", error_message: str = "", cooldown_seconds: int = 0) -> PhonePoolRecord | None:
        phone_norm = normalize_phone(phone)
        now = _utcnow()
        target = status if status in ALL_STATUSES else STATUS_ACTIVE
        with Session(engine) as session:
            model = session.exec(
                select(PhonePoolModel).where(PhonePoolModel.phone_e164 == phone_norm)
            ).first()
            if model is None:
                return None
            model.fail_count = int(model.fail_count or 0) + 1
            model.last_used_at = _now_text()
            model.last_error_code = str(error_code or "")[:64]
            model.last_error_message = str(error_message or "")[:500]
            if cooldown_seconds > 0:
                model.cooldown_until = (now + timedelta(seconds=int(cooldown_seconds))).isoformat().replace("+00:00", "Z")
            if target != STATUS_ACTIVE:
                model.status = target
            model.updated_at = now
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def record_probe_success(self, phone: str, *, reason: str = "") -> PhonePoolRecord | None:
        """短信探测成功：证明 OpenAI 已发码且收码 API 正常。

        这不是完整绑定，所以不增加 bound_count；但它足以恢复该号码自身状态，
        并通过号段聚合规则让该号段重新进入可用集合。
        """
        phone_norm = normalize_phone(phone)
        now = _utcnow()
        with Session(engine) as session:
            model = session.exec(
                select(PhonePoolModel).where(PhonePoolModel.phone_e164 == phone_norm)
            ).first()
            if model is None:
                return None
            model.status = STATUS_ACTIVE
            model.last_used_at = _now_text()
            model.last_error_code = ""
            model.last_error_message = ""
            model.cooldown_until = ""
            model.updated_at = now
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def record_phone_signup_prefix_status(
        self,
        phone: str,
        task_status: str,
        *,
        reason: str = "",
        email: str = "",
    ) -> dict[str, Any] | None:
        """记录手机号注册号段状态，但不修改手机号自身库存状态。"""
        phone_norm = normalize_phone(phone)
        prefix = _phone_prefix4(phone_norm)
        signal = _phone_signup_prefix_signal(task_status, reason)
        if not phone_norm or not prefix or not signal:
            return None
        try:
            _ensure_phone_prefix_state_schema()
        except Exception:
            # 让主链路不要因为号段状态表失败而失败。
            return None
        now = _utcnow()
        now_text = now.isoformat().replace("+00:00", "Z")
        status_code = str(task_status or "").strip()[:64]
        reason_text = str(reason or "")[:500]
        if signal == "failure" and "fraud_guard" in reason_text.lower() and status_code.lower() == "openai_rejected":
            status_code = "fraud_guard"
        with Session(engine) as session:
            model = session.exec(
                select(PhonePrefixStateModel)
                .where(PhonePrefixStateModel.purpose == PREFIX_PURPOSE_PHONE_SIGNUP)
                .where(PhonePrefixStateModel.prefix == prefix)
            ).first()
            if model is None:
                model = PhonePrefixStateModel(
                    purpose=PREFIX_PURPOSE_PHONE_SIGNUP,
                    prefix=prefix,
                    status=PREFIX_STATUS_UNTESTED,
                    created_at=now,
                )
            if signal == "success":
                model.success_count = int(model.success_count or 0) + 1
                model.last_success_phone = phone_norm
            else:
                model.failure_count = int(model.failure_count or 0) + 1
                model.last_failure_phone = phone_norm
                model.last_error_code = status_code
                model.last_error_message = reason_text
            model.status = _phone_signup_prefix_status_from_counts(model.success_count, model.failure_count)
            model.last_seen_at = now_text
            model.updated_at = now
            session.add(model)
            session.commit()
            session.refresh(model)
            return _prefix_state_to_item(model)

    def record_task_status(self, phone: str, task_status: str, *, reason: str = "", email: str = "") -> PhonePoolRecord | None:
        status = str(task_status or "")
        if status == "registered_phone_signup":
            return self.record_phone_signup_success(phone, email=email)
        if status == "bound":
            return self.record_success(phone, email=email)
        if status == "sms_probe_received":
            return self.record_probe_success(phone, reason=reason)
        if status == "openai_phone_limit":
            return self.record_failure(phone, status=STATUS_EXHAUSTED, error_code=status, error_message=reason)
        if status == "phone_already_used":
            return self.record_failure(phone, status=STATUS_EXHAUSTED, error_code=status, error_message=reason)
        if status in {"api_no_code", "api_error", "openai_rejected"}:
            return self.record_failure(phone, status=STATUS_CANNOT_SEND, error_code=status, error_message=reason)
        if status == "rate_limited":
            return self.record_failure(phone, status=STATUS_RATE_LIMITED, error_code=status, error_message=reason, cooldown_seconds=600)
        if status in {"account_phone_bound", "not_tested"}:
            return None
        if status in {"browser_error", "account_auth_error", "unknown"}:
            return self.record_failure(phone, status=STATUS_ACTIVE, error_code=status, error_message=reason)
        return None

    def reconcile_from_accounts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        api_urls: dict[str, str] = {}
        account_emails: dict[str, list[str]] = {}
        with Session(engine) as session:
            accounts = session.exec(select(AccountModel)).all()
            for account in accounts:
                try:
                    extra = account.get_extra()
                except Exception:
                    extra = {}
                binding = extra.get("chatgpt_phone_binding") if isinstance(extra, dict) else None
                if not isinstance(binding, dict) or str(binding.get("status") or "") != "bound":
                    continue
                phone = normalize_phone(str(binding.get("phone") or ""))
                if not phone:
                    continue
                counts[phone] = counts.get(phone, 0) + 1
                email = _normalize_email(account.email)
                if email:
                    account_emails.setdefault(phone, [])
                    if email not in account_emails[phone]:
                        account_emails[phone].append(email)
                api_url = str(binding.get("api_url") or "").strip()
                if api_url and phone not in api_urls:
                    api_urls[phone] = api_url
        updated = created = 0
        for phone, count in counts.items():
            before = self.get(phone)
            api_url = api_urls.get(phone, "")
            if before is None and api_url:
                self.add(phone=phone, api_url=api_url)
                created += 1
            rec = self.get(phone)
            if rec:
                with Session(engine) as session:
                    model = session.get(PhonePoolModel, rec.id)
                    if model:
                        model.bound_count = count
                        model.bound_account_emails_json = _dump_email_list(account_emails.get(phone, []))
                        if int(model.bound_count or 0) >= int(model.max_accounts or 3) and model.status == STATUS_ACTIVE:
                            model.status = STATUS_EXHAUSTED
                        model.updated_at = _utcnow()
                        session.add(model)
                        session.commit()
                        updated += 1
        return {"counted_phones": len(counts), "created": created, "updated": updated}


_expiry_autofill_started = False


def start_phone_pool_api_expiry_autofill(*, delay_seconds: float = 20.0, limit: int = 50) -> bool:
    """启动后空闲补一次手机号池 API 到期时间空值。

    只处理没有 api_expired_date 且之前没有失败过的记录；有效期固定，不做周期刷新。
    """
    global _expiry_autofill_started
    if _expiry_autofill_started:
        return False
    _expiry_autofill_started = True

    def run() -> None:
        try:
            time.sleep(max(float(delay_seconds or 0), 0.0))
            result = PhonePoolRepository().refresh_missing_api_expiry(limit=limit)
            summary = result.get("summary") if isinstance(result, dict) else {}
            checked = int((summary or {}).get("checked") or 0)
            if checked:
                print(
                    "[PhonePool] API 到期时间后台补全完成: "
                    f"checked={checked} success={int((summary or {}).get('success') or 0)} "
                    f"missing={int((summary or {}).get('missing_expired_date') or 0)} "
                    f"error={int((summary or {}).get('error') or 0)}"
                )
        except Exception as exc:
            print(f"[PhonePool] API 到期时间后台补全失败: {exc}")

    thread = threading.Thread(target=run, name="phone-pool-api-expiry-autofill", daemon=True)
    thread.start()
    return True
