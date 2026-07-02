from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
import string
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import Session, select, func

from core.config_store import config_store
from core.db import (
    AccountModel,
    DeliveryCardBatchModel,
    DeliveryCardEventModel,
    DeliveryCardModel,
    DeliveryRedeemApiLogModel,
    DeliverySkuModel,
    PhonePoolModel,
    engine,
)
from services.account_filters import (
    account_subscription_active_until_timestamp,
    account_subscription_type,
    account_validity,
)

STATUS_UNUSED = "unused"
STATUS_REDEEMED = "redeemed"
STATUS_DISABLED = "disabled"
STATUS_EXPIRED = "expired"
STATUS_BLOCKED = "blocked"

EVENT_FIRST_REDEEM = "first_redeem"
EVENT_REFETCH = "refetch"
EVENT_FAILED = "failed"

RESULT_SUCCESS = "success"
RESULT_FAILED = "failed"

ERROR_CARD_NOT_AVAILABLE = "CARD_NOT_AVAILABLE"
ERROR_CARD_DISABLED = "CARD_DISABLED"
ERROR_CARD_EXPIRED = "CARD_EXPIRED"
ERROR_SKU_DISABLED = "SKU_DISABLED"
ERROR_POOL_EMPTY = "POOL_EMPTY"
ERROR_DUPLICATE_ACCOUNT_DETECTED = "DUPLICATE_ACCOUNT_DETECTED"
ERROR_ACCOUNT_ALLOCATION_FAILED = "ACCOUNT_ALLOCATION_FAILED"
ERROR_REFETCH_LIMIT_EXCEEDED = "REFETCH_LIMIT_EXCEEDED"
ERROR_API_DISABLED = "API_DISABLED"
ERROR_UNAUTHORIZED = "UNAUTHORIZED"
ERROR_INVALID_REQUEST = "INVALID_REQUEST"
ERROR_RATE_LIMITED = "RATE_LIMITED"

DELIVERY_CONFIG_KEYS = {
    "delivery_cards_api_enabled": "false",
    "delivery_cards_api_rate_limit_per_minute": "60",
    "delivery_cards_api_failed_block_threshold": "12",
    "delivery_cards_api_failed_block_minutes": "15",
    "delivery_cards_api_failure_mode": "safe",
    "delivery_cards_allow_refetch_default": "true",
    "delivery_cards_max_refetch_count_default": "0",
    "delivery_cards_plus_sort_policy": "earliest_expiry",
    "delivery_cards_free_sort_policy": "oldest_created",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


def safe_json_loads(value: Any, default: Any = None) -> Any:
    if default is None:
        default = {}
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value or ""))
        return parsed
    except Exception:
        return default


def safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def parse_bool(value: Any, default: bool = False) -> bool:
    text_value = str(value if value is not None else "").strip().lower()
    if not text_value:
        return default
    if text_value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text_value in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def normalize_code(value: str) -> str:
    text_value = str(value or "").strip().upper()
    # Keep human separators predictable while stripping spaces.
    return "".join(ch for ch in text_value if ch not in {" ", "\t", "\n", "\r"})


def code_prefix(value: str) -> str:
    normalized = normalize_code(value)
    if "-" in normalized:
        return normalized.split("-", 1)[0]
    return "".join(ch for ch in normalized if ch.isalpha())[:12]


def _hash_secret() -> str:
    secret = config_store.get("delivery_cards_code_hash_secret", "")
    if not secret:
        secret = secrets.token_hex(32)
        config_store.set("delivery_cards_code_hash_secret", secret)
    return secret


def hash_code(value: str) -> str:
    normalized = normalize_code(value)
    secret = _hash_secret()
    return hashlib.sha256(f"{secret}:{normalized}".encode("utf-8")).hexdigest()


def hash_token(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    secret = _hash_secret()
    return hashlib.sha256(f"delivery-token:{secret}:{token}".encode("utf-8")).hexdigest()


def mask_code(value: str) -> str:
    normalized = normalize_code(value)
    if not normalized:
        return ""
    parts = normalized.split("-")
    if len(parts) >= 3:
        return "-".join([parts[0], parts[1], "****", parts[-1]])
    if len(normalized) <= 10:
        return f"{normalized[:2]}****{normalized[-2:]}"
    return f"{normalized[:8]}****{normalized[-4:]}"


def generate_code(prefix: str) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4)]
    return f"{str(prefix or '').strip().upper()}-" + "-".join(groups)


def trace_id() -> str:
    return f"redeem_{utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"


def get_delivery_settings() -> dict[str, Any]:
    values = {key: config_store.get(key, default) for key, default in DELIVERY_CONFIG_KEYS.items()}
    token_hash = config_store.get("delivery_cards_api_token_hash", "")
    token_last4 = config_store.get("delivery_cards_api_token_last4", "")
    values.update({
        "api_enabled": parse_bool(values.get("delivery_cards_api_enabled")),
        "token_configured": bool(token_hash),
        "token_last4": token_last4,
        "rate_limit_per_minute": safe_int(values.get("delivery_cards_api_rate_limit_per_minute"), 60),
        "failed_block_threshold": safe_int(values.get("delivery_cards_api_failed_block_threshold"), 12),
        "failed_block_minutes": safe_int(values.get("delivery_cards_api_failed_block_minutes"), 15),
        "failure_mode": str(values.get("delivery_cards_api_failure_mode") or "safe"),
    })
    return values


def update_delivery_settings(data: dict[str, Any]) -> dict[str, Any]:
    allowed = set(DELIVERY_CONFIG_KEYS)
    safe: dict[str, str] = {}
    for key, value in (data or {}).items():
        if key not in allowed:
            continue
        safe[key] = str(value if value is not None else "")
    if safe:
        config_store.set_many(safe)
        with Session(engine) as session:
            plus_policy = safe.get("delivery_cards_plus_sort_policy")
            free_policy = safe.get("delivery_cards_free_sort_policy")
            if plus_policy:
                sku = session.exec(select(DeliverySkuModel).where(DeliverySkuModel.code == "plus")).first()
                if sku:
                    sku.sort_policy = str(plus_policy)
                    sku.updated_at = utcnow()
                    session.add(sku)
            if free_policy:
                sku = session.exec(select(DeliverySkuModel).where(DeliverySkuModel.code == "free")).first()
                if sku:
                    sku.sort_policy = str(free_policy)
                    sku.updated_at = utcnow()
                    session.add(sku)
            session.commit()
    return get_delivery_settings()


def rotate_api_token() -> dict[str, Any]:
    token = "dc_live_" + secrets.token_urlsafe(32)
    config_store.set_many({
        "delivery_cards_api_token_hash": hash_token(token),
        "delivery_cards_api_token_last4": token[-4:],
    })
    return {"ok": True, "token": token, "last4": token[-4:]}



_RATE_LOCK = threading.Lock()
_RATE_STATE: dict[str, dict[str, float]] = {}
_NON_MALICIOUS_FAILURE_CODES = {
    ERROR_POOL_EMPTY,
    ERROR_DUPLICATE_ACCOUNT_DETECTED,
    ERROR_ACCOUNT_ALLOCATION_FAILED,
}


def _extract_bearer_token(authorization: str) -> str:
    token = str(authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _rate_limit_identity(*, authorization: str, client_ip: str, consumer: str = "") -> str:
    token = _extract_bearer_token(authorization)
    if token:
        token_fingerprint = hash_token(token)[:16]
    else:
        token_fingerprint = "no-token"
    return f"{str(client_ip or '-').strip()[:120]}:{str(consumer or '').strip()[:80]}:{token_fingerprint}"


def check_api_rate_limit(identity: str) -> None:
    settings = get_delivery_settings()
    limit = max(1, safe_int(settings.get("rate_limit_per_minute"), 60))
    now = time.time()
    with _RATE_LOCK:
        item = _RATE_STATE.setdefault(identity, {})
        blocked_until = float(item.get("blocked_until", 0) or 0)
        if blocked_until > now:
            seconds = max(1, int(blocked_until - now))
            raise DeliveryRedeemError(ERROR_RATE_LIMITED, f"请求过于频繁，已临时阻断，请 {seconds} 秒后重试", action="rate_limited")
        window_start = float(item.get("window_start", 0) or 0)
        if now - window_start >= 60:
            item["window_start"] = now
            item["count"] = 0
        item["count"] = float(item.get("count", 0) or 0) + 1
        if item["count"] > limit:
            item["blocked_until"] = now + 60
            raise DeliveryRedeemError(ERROR_RATE_LIMITED, "请求超过每分钟上限，请稍后重试", action="rate_limited")


def record_api_failure(identity: str, error_code: str) -> None:
    code = str(error_code or "")
    if not identity or code in _NON_MALICIOUS_FAILURE_CODES:
        return
    settings = get_delivery_settings()
    threshold = max(0, safe_int(settings.get("failed_block_threshold"), 12))
    block_minutes = max(1, safe_int(settings.get("failed_block_minutes"), 15))
    if threshold <= 0:
        return
    now = time.time()
    with _RATE_LOCK:
        item = _RATE_STATE.setdefault(identity, {})
        failed_window_start = float(item.get("failed_window_start", 0) or 0)
        if now - failed_window_start >= 300:
            item["failed_window_start"] = now
            item["failed_count"] = 0
        item["failed_count"] = float(item.get("failed_count", 0) or 0) + 1
        if item["failed_count"] >= threshold:
            item["blocked_until"] = max(float(item.get("blocked_until", 0) or 0), now + block_minutes * 60)


def record_api_success(identity: str) -> None:
    if not identity:
        return
    with _RATE_LOCK:
        item = _RATE_STATE.setdefault(identity, {})
        item["failed_count"] = 0
        item["failed_window_start"] = time.time()


def verify_candidate_api_token(token: str) -> bool:
    expected = str(config_store.get("delivery_cards_api_token_hash", "") or "").strip()
    candidate = _extract_bearer_token(token)
    if not expected or not candidate:
        return False
    return secrets.compare_digest(hash_token(candidate), expected)


def verify_api_token(authorization: str) -> bool:
    settings = get_delivery_settings()
    if not settings.get("api_enabled"):
        raise DeliveryRedeemError(ERROR_API_DISABLED, "外部兑换 API 未启用", action="auth_failed")
    expected = str(config_store.get("delivery_cards_api_token_hash", "") or "").strip()
    if not expected:
        raise DeliveryRedeemError(ERROR_UNAUTHORIZED, "外部兑换 API Token 未配置", action="auth_failed")
    token = _extract_bearer_token(authorization)
    if not token or not secrets.compare_digest(hash_token(token), expected):
        raise DeliveryRedeemError(ERROR_UNAUTHORIZED, "外部兑换 API Token 无效", action="auth_failed")
    return True


def public_error_code(code: str) -> str:
    mode = str(config_store.get("delivery_cards_api_failure_mode", "safe") or "safe").strip().lower()
    if mode != "debug" and code in {ERROR_CARD_DISABLED, ERROR_CARD_EXPIRED, ERROR_CARD_NOT_AVAILABLE}:
        return ERROR_CARD_NOT_AVAILABLE
    return code


def row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, "model_dump"):
        return row.model_dump(mode="json")
    return row.dict()


def get_skus(session: Session) -> list[DeliverySkuModel]:
    rows = session.exec(select(DeliverySkuModel).order_by(DeliverySkuModel.id.asc())).all()
    for row in rows:
        if row.code == "plus":
            row.sort_policy = config_store.get("delivery_cards_plus_sort_policy", row.sort_policy or "earliest_expiry")
        elif row.code == "free":
            row.sort_policy = config_store.get("delivery_cards_free_sort_policy", row.sort_policy or "oldest_created")
    return rows


def account_extra(account: AccountModel) -> dict[str, Any]:
    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    return extra if isinstance(extra, dict) else {}


def assigned_account_ids(session: Session) -> set[int]:
    rows = session.exec(
        select(DeliveryCardModel.assigned_account_id).where(DeliveryCardModel.assigned_account_id > 0)
    ).all()
    return {int(value or 0) for value in rows if int(value or 0) > 0}


def is_account_eligible_for_sku(account: AccountModel, sku_code: str, assigned_ids: set[int] | None = None) -> bool:
    if str(account.platform or "").strip() != "chatgpt":
        return False
    if not str(account.email or "").strip() or not str(account.password or "").strip():
        return False
    if str(account.status or "").strip().lower() == "invalid":
        return False
    extra = account_extra(account)
    if bool(extra.get("manually_used")):
        return False
    if assigned_ids is not None and int(account.id or 0) in assigned_ids:
        return False
    if account_validity(account, extra) != "valid":
        return False
    subscription_type = account_subscription_type(account, extra)
    if sku_code == "plus":
        if subscription_type != "plus":
            return False
        expiry = account_subscription_active_until_timestamp(account, extra)
        if expiry is not None and expiry < time.time():
            return False
        return True
    if sku_code == "free":
        # Deliberately exclude unknown; do not sell unclear accounts as FREE.
        return subscription_type == "free"
    return subscription_type == sku_code


def account_sort_key(account: AccountModel, sku: DeliverySkuModel) -> tuple[int, float, float, int]:
    policy = str(sku.sort_policy or "").strip().lower()
    extra = account_extra(account)
    expiry = account_subscription_active_until_timestamp(account, extra)
    created_ts = account.created_at.timestamp() if account.created_at else 0
    account_id = int(account.id or 0)
    if policy in {"earliest_expiry", "oldest_subscription_started"}:
        # `oldest_subscription_started` falls back to expiry until the project records started_at durably.
        return (0 if expiry is not None else 1, expiry or 0, created_ts, account_id)
    return (0, created_ts, created_ts, account_id)


def pick_next_account_for_sku(session: Session, sku: DeliverySkuModel) -> tuple[AccountModel | None, int]:
    rows = session.exec(
        select(AccountModel).where(AccountModel.platform == sku.platform).order_by(AccountModel.id.asc())
    ).all()
    used_ids = assigned_account_ids(session)
    candidates = [row for row in rows if is_account_eligible_for_sku(row, sku.code, used_ids)]
    candidates.sort(key=lambda row: account_sort_key(row, sku))
    return (candidates[0] if candidates else None, len(candidates))


def available_account_count(session: Session, sku: DeliverySkuModel) -> int:
    rows = session.exec(select(AccountModel).where(AccountModel.platform == sku.platform)).all()
    used_ids = assigned_account_ids(session)
    return sum(1 for row in rows if is_account_eligible_for_sku(row, sku.code, used_ids))


def unused_card_count(session: Session, sku_code: str) -> int:
    return int(session.exec(
        select(func.count()).select_from(DeliveryCardModel).where(
            DeliveryCardModel.sku_code == sku_code,
            DeliveryCardModel.status == STATUS_UNUSED,
        )
    ).one() or 0)


def serialize_sku(sku: DeliverySkuModel) -> dict[str, Any]:
    return row_to_dict(sku)


def stock_summary(session: Session) -> dict[str, Any]:
    items = []
    for sku in get_skus(session):
        available = available_account_count(session, sku)
        unused = unused_card_count(session, sku.code)
        next_account, _candidate_count = pick_next_account_for_sku(session, sku)
        next_payload = None
        if next_account is not None:
            next_payload = {
                "id": int(next_account.id or 0),
                "email": next_account.email,
                "subscription_active_until": subscription_active_until_iso(next_account),
                "status": next_account.status,
            }
        items.append({
            "sku_code": sku.code,
            "name": sku.name,
            "prefix": sku.code_prefix,
            "platform": sku.platform,
            "enabled": bool(sku.enabled),
            "delivery_profile": sku.delivery_profile,
            "sort_policy": sku.sort_policy,
            "available_accounts": available,
            "unused_cards": unused,
            "stock_delta": available - unused,
            "risk": available - unused < 0,
            "next_account": next_payload,
        })
    return {"items": items}



def _normalize_phone_e164(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return f"+{digits}" if digits else ""


def _api_url_from_phone_pool(phone: str) -> str:
    normalized = _normalize_phone_e164(phone)
    if not normalized:
        return ""
    try:
        with Session(engine) as session:
            record = session.exec(select(PhonePoolModel).where(PhonePoolModel.phone_e164 == normalized)).first()
            return str(record.api_url or "").strip() if record else ""
    except Exception:
        return ""


def account_phone_binding_payload(account: AccountModel) -> dict[str, Any]:
    extra = account_extra(account)
    binding = extra.get("chatgpt_phone_binding") if isinstance(extra.get("chatgpt_phone_binding"), dict) else {}
    phone = _normalize_phone_e164(binding.get("phone") if isinstance(binding, dict) else "")
    raw_line = str(binding.get("raw_line") or "").strip() if isinstance(binding, dict) else ""
    api_url = str(binding.get("api_url") or binding.get("sms_api") or "").strip() if isinstance(binding, dict) else ""
    if not api_url and raw_line and "----" in raw_line:
        _phone_part, api_part = raw_line.split("----", 1)
        api_url = str(api_part or "").strip()
    if not api_url and phone:
        api_url = _api_url_from_phone_pool(phone)
    if not phone:
        return {
            "bound": False,
            "status": "missing",
            "phone": "",
            "phone_e164": "",
            "api_url": "",
            "sms_api": "",
            "message": "该账号没有手机号绑定记录",
        }
    return {
        "bound": True,
        "status": str(binding.get("status") or "bound").strip() if isinstance(binding, dict) else "bound",
        "status_label": str(binding.get("status_label") or "已绑定").strip() if isinstance(binding, dict) else "已绑定",
        "phone": phone,
        "phone_e164": phone,
        "api_url": api_url,
        "sms_api": api_url,
        "raw_line": raw_line,
        "bound_at": str(binding.get("bound_at") or binding.get("updated_at") or binding.get("created_at") or "").strip() if isinstance(binding, dict) else "",
        "api_expired_date": str(binding.get("api_expired_date") or "").strip() if isinstance(binding, dict) else "",
        "code_time": str(binding.get("code_time") or "").strip() if isinstance(binding, dict) else "",
        "task_id": str(binding.get("task_id") or "").strip() if isinstance(binding, dict) else "",
        "source": str(binding.get("source") or "").strip() if isinstance(binding, dict) else "",
        "message": "",
    }


def subscription_active_until_iso(account: AccountModel) -> str:
    extra = account_extra(account)
    local = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    sub = local.get("subscription") if isinstance(local.get("subscription"), dict) else {}
    for candidate in (
        sub.get("subscription_active_until"),
        sub.get("subscription_expires_at_iso"),
        sub.get("subscription_expires_at"),
        extra.get("subscription_active_until"),
        extra.get("subscription_expires_at"),
        extra.get("chatgpt_subscription_active_until"),
    ):
        text_value = str(candidate or "").strip()
        if text_value:
            return text_value
    return ""


def build_delivery_payload(account: AccountModel, sku: DeliverySkuModel) -> dict[str, Any]:
    extra = account_extra(account)
    subscription_type = account_subscription_type(account, extra)
    phone_binding = account_phone_binding_payload(account)
    base = {
        "platform": account.platform,
        "email": account.email,
        "password": account.password,
        "subscription_type": subscription_type,
        "phone": phone_binding.get("phone", "") if phone_binding.get("bound") else "",
        "sms_api": phone_binding.get("sms_api", "") if phone_binding.get("bound") else "",
        "phone_binding": phone_binding,
        "phone_binding_message": phone_binding.get("message", ""),
    }
    if sku.delivery_profile == "chatgpt_basic":
        base.update({
            "status": account.status,
            "subscription_active_until": subscription_active_until_iso(account),
        })
    return base


def mark_account_assigned(account: AccountModel, card: DeliveryCardModel, now: str) -> None:
    extra = account_extra(account)
    extra["manually_used"] = True
    extra["delivery_card_assignment"] = {
        "card_id": int(card.id or 0),
        "batch_id": int(card.batch_id or 0),
        "sku_code": card.sku_code,
        "code_mask": card.code_mask,
        "assigned_at": now,
        "source": "delivery_card_api",
    }
    account.extra_json = safe_json_dumps(extra)
    account.updated_at = utcnow()


@dataclass
class DuplicateCheckResult:
    ok: bool
    status: str
    message: str
    duplicate_card_ids: list[int] = field(default_factory=list)
    duplicate_event_ids: list[int] = field(default_factory=list)
    duplicate_api_log_ids: list[int] = field(default_factory=list)

    def to_log_patch(self) -> dict[str, Any]:
        return {
            "duplicate_check_status": self.status,
            "duplicate_check_message": self.message,
            "duplicate_card_ids_json": safe_json_dumps(self.duplicate_card_ids),
            "duplicate_event_ids_json": safe_json_dumps(self.duplicate_event_ids),
            "duplicate_api_log_ids_json": safe_json_dumps(self.duplicate_api_log_ids),
        }


def assert_no_delivery_duplicate(session: Session, *, account: AccountModel, current_card_id: int) -> DuplicateCheckResult:
    account_id = int(account.id or 0)
    extra = account_extra(account)
    assignment = extra.get("delivery_card_assignment") if isinstance(extra.get("delivery_card_assignment"), dict) else {}
    if bool(extra.get("manually_used")):
        assigned_card_id = safe_int(assignment.get("card_id"), 0)
        if assigned_card_id != int(current_card_id or 0):
            return DuplicateCheckResult(False, "failed", "账号已标记为已使用", duplicate_card_ids=[assigned_card_id] if assigned_card_id else [])

    dup_cards = [int(row.id or 0) for row in session.exec(
        select(DeliveryCardModel).where(
            DeliveryCardModel.assigned_account_id == account_id,
            DeliveryCardModel.id != current_card_id,
            DeliveryCardModel.status.in_([STATUS_REDEEMED, STATUS_DISABLED, STATUS_BLOCKED]),
        )
    ).all()]
    dup_events = [int(row.id or 0) for row in session.exec(
        select(DeliveryCardEventModel).where(
            DeliveryCardEventModel.account_id == account_id,
            DeliveryCardEventModel.result == RESULT_SUCCESS,
            DeliveryCardEventModel.card_id != current_card_id,
        )
    ).all()]
    dup_logs = [int(row.id or 0) for row in session.exec(
        select(DeliveryRedeemApiLogModel).where(
            DeliveryRedeemApiLogModel.assigned_account_id == account_id,
            DeliveryRedeemApiLogModel.result == RESULT_SUCCESS,
            DeliveryRedeemApiLogModel.card_id != current_card_id,
        )
    ).all()]
    if dup_cards or dup_events or dup_logs:
        return DuplicateCheckResult(
            False,
            "failed",
            "账号已存在其他交付记录",
            duplicate_card_ids=dup_cards,
            duplicate_event_ids=dup_events,
            duplicate_api_log_ids=dup_logs,
        )
    return DuplicateCheckResult(True, "passed", "未发现该账号在兑换 API 中被其他卡密交付")


class DeliveryRedeemError(Exception):
    def __init__(self, code: str, message: str, *, action: str = "failed", context: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action
        self.context = context or {}


def write_api_log(session: Session, payload: dict[str, Any]) -> DeliveryRedeemApiLogModel:
    log = DeliveryRedeemApiLogModel(**payload)
    session.add(log)
    return log


def write_failed_api_log(payload: dict[str, Any]) -> None:
    with Session(engine) as session:
        log = DeliveryRedeemApiLogModel(**payload)
        session.add(log)
        session.commit()


def make_api_log_payload(
    *,
    trace: str,
    started_at: float,
    request_id: str = "",
    idempotency_key: str = "",
    consumer: str = "",
    api_token_id: str = "",
    client_ip: str = "",
    user_agent: str = "",
    code: str = "",
    card: DeliveryCardModel | None = None,
    sku_code: str = "",
    account: AccountModel | None = None,
    action: str = "failed",
    result: str = RESULT_FAILED,
    error_code: str = "",
    redeem_index: int = 0,
    first_redeem: bool = False,
    idempotent_replay: bool = False,
    duplicate: DuplicateCheckResult | None = None,
    stock_before: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    response_summary: dict[str, Any] | None = None,
    message: str = "",
) -> dict[str, Any]:
    normalized = normalize_code(code)
    hashed = hash_code(normalized) if normalized else ""
    patch = duplicate.to_log_patch() if duplicate else {}
    return {
        "trace_id": trace,
        "request_id": str(request_id or "")[:200],
        "idempotency_key": str(idempotency_key or "")[:200],
        "consumer": str(consumer or "")[:120],
        "api_token_id": str(api_token_id or "")[:120],
        "client_ip": str(client_ip or "")[:120],
        "user_agent": str(user_agent or "")[:500],
        "code_prefix": (card.code_prefix if card else code_prefix(normalized))[:50],
        "code_mask": (card.code_mask if card else mask_code(normalized))[:120],
        "code_hash_prefix": hashed[:12],
        "card_id": int(card.id or 0) if card else 0,
        "batch_id": int(card.batch_id or 0) if card else 0,
        "sku_code": (card.sku_code if card else sku_code) or "",
        "assigned_account_id": int(account.id or 0) if account else 0,
        "assigned_account_email": str(account.email or "") if account else "",
        "action": action,
        "result": result,
        "error_code": error_code,
        "redeem_index": int(redeem_index or 0),
        "first_redeem": bool(first_redeem),
        "idempotent_replay": bool(idempotent_replay),
        "duplicate_check_status": patch.get("duplicate_check_status", ""),
        "duplicate_check_message": patch.get("duplicate_check_message", ""),
        "duplicate_card_ids_json": patch.get("duplicate_card_ids_json", "[]"),
        "duplicate_event_ids_json": patch.get("duplicate_event_ids_json", "[]"),
        "duplicate_api_log_ids_json": patch.get("duplicate_api_log_ids_json", "[]"),
        "stock_before_json": safe_json_dumps(stock_before or {}),
        "decision_json": safe_json_dumps(decision or {}),
        "response_summary_json": safe_json_dumps(response_summary or {}),
        "message": message,
        "duration_ms": max(0, int((time.time() - started_at) * 1000)),
    }


def serialize_event(event: DeliveryCardEventModel) -> dict[str, Any]:
    return row_to_dict(event)


def serialize_api_log(log: DeliveryRedeemApiLogModel) -> dict[str, Any]:
    data = row_to_dict(log)
    for key in ("duplicate_card_ids_json", "duplicate_event_ids_json", "duplicate_api_log_ids_json", "stock_before_json", "decision_json", "response_summary_json"):
        data[key[:-5] if key.endswith("_json") else key] = safe_json_loads(data.get(key), [] if "ids" in key else {})
    return data


def serialize_card(card: DeliveryCardModel, *, include_payload: bool = False) -> dict[str, Any]:
    data = row_to_dict(card)
    if include_payload:
        data["delivery_payload"] = safe_json_loads(card.delivery_payload_json, {})
    else:
        data.pop("delivery_payload_json", None)
    return data


def lookup_card_by_code(session: Session, code: str) -> dict[str, Any]:
    normalized = normalize_code(code)
    if not normalized:
        raise HTTPException(400, "请输入完整卡密")
    card = session.exec(select(DeliveryCardModel).where(DeliveryCardModel.code_hash == hash_code(normalized))).first()
    if not card:
        raise HTTPException(404, "卡密不存在")
    detail = card_detail(session, int(card.id or 0))
    detail["lookup"] = {"matched": True, "code_mask": card.code_mask, "code_prefix": card.code_prefix}
    return detail


def _account_assignment_state(account: AccountModel | None) -> dict[str, Any]:
    if account is None:
        return {"exists": False, "manually_used": False, "assignment": {}}
    extra = account_extra(account)
    assignment = extra.get("delivery_card_assignment") if isinstance(extra.get("delivery_card_assignment"), dict) else {}
    return {"exists": True, "manually_used": bool(extra.get("manually_used")), "assignment": assignment}


def scan_consistency(session: Session) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    cards = session.exec(select(DeliveryCardModel).order_by(DeliveryCardModel.id.asc())).all()
    assigned_map: dict[int, list[DeliveryCardModel]] = {}
    for card in cards:
        account_id = int(card.assigned_account_id or 0)
        if account_id > 0:
            assigned_map.setdefault(account_id, []).append(card)
        if card.status == STATUS_REDEEMED and account_id <= 0:
            issues.append({"type": "redeemed_without_account", "severity": "critical", "repairable": False, "card_id": int(card.id or 0), "message": "已兑换卡密没有绑定账号"})
        if account_id > 0:
            account = session.get(AccountModel, account_id)
            state = _account_assignment_state(account)
            if not state["exists"]:
                issues.append({"type": "assigned_account_missing", "severity": "critical", "repairable": False, "card_id": int(card.id or 0), "account_id": account_id, "message": "卡密绑定的账号不存在"})
                continue
            assignment = state["assignment"]
            assignment_card_id = safe_int(assignment.get("card_id"), 0) if isinstance(assignment, dict) else 0
            if not state["manually_used"] or assignment_card_id != int(card.id or 0):
                issues.append({
                    "type": "account_usage_marker_missing",
                    "severity": "high",
                    "repairable": True,
                    "card_id": int(card.id or 0),
                    "account_id": account_id,
                    "message": "卡密已绑定账号，但账号未正确标记 manually_used / delivery_card_assignment",
                })
            if card.status == STATUS_UNUSED:
                issues.append({"type": "unused_card_has_account", "severity": "high", "repairable": True, "card_id": int(card.id or 0), "account_id": account_id, "message": "未兑换卡密却已经绑定账号，启用/修复后应恢复为已兑换"})
    for account_id, rows in assigned_map.items():
        active_rows = [row for row in rows if row.status in {STATUS_UNUSED, STATUS_REDEEMED, STATUS_DISABLED, STATUS_BLOCKED}]
        if len(active_rows) > 1:
            issues.append({
                "type": "duplicate_assigned_account",
                "severity": "critical",
                "repairable": False,
                "account_id": account_id,
                "card_ids": [int(row.id or 0) for row in active_rows],
                "message": "同一个账号被多个交付卡密占用",
            })
    success_logs = session.exec(select(DeliveryRedeemApiLogModel).where(DeliveryRedeemApiLogModel.result == RESULT_SUCCESS)).all()
    for log in success_logs:
        card_id = int(log.card_id or 0)
        account_id = int(log.assigned_account_id or 0)
        if not card_id:
            continue
        card = session.get(DeliveryCardModel, card_id)
        if not card:
            issues.append({"type": "success_log_card_missing", "severity": "medium", "repairable": False, "api_log_id": int(log.id or 0), "card_id": card_id, "message": "成功 API 日志关联的卡密不存在"})
            continue
        if account_id and int(card.assigned_account_id or 0) != account_id:
            issues.append({"type": "success_log_account_mismatch", "severity": "critical", "repairable": False, "api_log_id": int(log.id or 0), "card_id": card_id, "account_id": account_id, "card_account_id": int(card.assigned_account_id or 0), "message": "成功 API 日志与卡密绑定账号不一致"})
        if card.status == STATUS_UNUSED and int(card.assigned_account_id or 0) > 0:
            issues.append({"type": "success_log_card_status_unredeemed", "severity": "high", "repairable": True, "api_log_id": int(log.id or 0), "card_id": card_id, "message": "成功 API 日志存在，但卡密状态仍是未兑换"})
    repairable = sum(1 for issue in issues if issue.get("repairable"))
    by_type: dict[str, int] = {}
    for issue in issues:
        by_type[str(issue.get("type") or "unknown")] = by_type.get(str(issue.get("type") or "unknown"), 0) + 1
    return {"ok": not issues, "issue_count": len(issues), "repairable_count": repairable, "by_type": by_type, "issues": issues[:500]}


def repair_consistency(session: Session) -> dict[str, Any]:
    before = scan_consistency(session)
    repaired: list[dict[str, Any]] = []
    seen_cards: set[int] = set()
    now = iso_now()
    for issue in before.get("issues", []):
        if not issue.get("repairable"):
            continue
        card_id = safe_int(issue.get("card_id"), 0)
        if not card_id or card_id in seen_cards:
            continue
        card = session.get(DeliveryCardModel, card_id)
        if not card or int(card.assigned_account_id or 0) <= 0:
            continue
        account = session.get(AccountModel, int(card.assigned_account_id or 0))
        if not account:
            continue
        if card.status == STATUS_UNUSED:
            card.status = STATUS_REDEEMED
            if not card.assigned_at:
                card.assigned_at = now
            if not card.first_redeemed_at:
                card.first_redeemed_at = card.assigned_at or now
            if not card.last_redeemed_at:
                card.last_redeemed_at = card.first_redeemed_at or now
            card.redeem_count = max(1, int(card.redeem_count or 0))
        mark_account_assigned(account, card, card.assigned_at or now)
        card.updated_at = utcnow()
        session.add(card)
        session.add(account)
        session.add(DeliveryCardEventModel(
            card_id=int(card.id or 0),
            batch_id=int(card.batch_id or 0),
            sku_code=card.sku_code,
            account_id=int(card.assigned_account_id or 0),
            event_type="admin_consistency_repair",
            result=RESULT_SUCCESS,
            message="一致性修复：恢复卡密状态/账号已使用标记",
            detail_json=safe_json_dumps({"issue_type": issue.get("type")}),
        ))
        repaired.append({"card_id": card_id, "account_id": int(card.assigned_account_id or 0), "issue_type": issue.get("type")})
        seen_cards.add(card_id)
    session.commit()
    after = scan_consistency(session)
    return {"ok": True, "before": before, "after": after, "repaired": repaired, "repaired_count": len(repaired)}


def serialize_batch(batch: DeliveryCardBatchModel, session: Session) -> dict[str, Any]:
    data = row_to_dict(batch)
    counts = {}
    rows = session.exec(
        select(DeliveryCardModel.status, func.count()).where(DeliveryCardModel.batch_id == int(batch.id or 0)).group_by(DeliveryCardModel.status)
    ).all()
    for status, count in rows:
        counts[str(status or "unknown")] = int(count or 0)
    data["counts"] = counts
    data["unused_count"] = counts.get(STATUS_UNUSED, 0)
    data["redeemed_count"] = counts.get(STATUS_REDEEMED, 0)
    data["disabled_count"] = counts.get(STATUS_DISABLED, 0)
    data["expired_count"] = counts.get(STATUS_EXPIRED, 0)
    return data


def create_batch(session: Session, *, name: str, sku_code: str, count: int, strict_stock_check: bool = True, expires_at: str = "", note: str = "") -> dict[str, Any]:
    sku = session.exec(select(DeliverySkuModel).where(DeliverySkuModel.code == sku_code)).first()
    if not sku or not bool(sku.enabled):
        raise HTTPException(400, "SKU 不存在或已停用")
    count_value = max(1, min(int(count or 0), 5000))
    available = available_account_count(session, sku)
    unused = unused_card_count(session, sku.code)
    if strict_stock_check and available < unused + count_value:
        raise HTTPException(400, f"{sku.name} 可用账号 {available} 个，当前未兑换卡密 {unused} 张，本次生成 {count_value} 张后库存不足")
    batch = DeliveryCardBatchModel(
        name=str(name or f"{utcnow().strftime('%Y-%m-%d')} {sku.code_prefix} 批次").strip(),
        sku_code=sku.code,
        platform=sku.platform,
        code_prefix=sku.code_prefix,
        total_count=count_value,
        strict_stock_check=bool(strict_stock_check),
        expires_at=str(expires_at or "").strip(),
        note=str(note or "").strip(),
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    codes: list[dict[str, str]] = []
    for _ in range(count_value):
        for _attempt in range(20):
            code = generate_code(sku.code_prefix)
            hashed = hash_code(code)
            exists = session.exec(select(DeliveryCardModel).where(DeliveryCardModel.code_hash == hashed)).first()
            if not exists:
                break
        else:
            raise HTTPException(500, "生成卡密失败，请重试")
        card = DeliveryCardModel(
            batch_id=int(batch.id or 0),
            sku_code=sku.code,
            platform=sku.platform,
            code_hash=hashed,
            code_mask=mask_code(code),
            code_prefix=sku.code_prefix,
            status=STATUS_UNUSED,
            expires_at=batch.expires_at,
        )
        session.add(card)
        codes.append({"code": code, "code_mask": card.code_mask, "sku_code": sku.code, "batch_name": batch.name, "expires_at": batch.expires_at})
    session.commit()
    return {"ok": True, "batch": serialize_batch(batch, session), "codes": codes, "csv": codes_to_csv(codes)}


def codes_to_csv(codes: list[dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["code", "sku_code", "batch_name", "expires_at"])
    writer.writeheader()
    for item in codes:
        writer.writerow({
            "code": item.get("code", ""),
            "sku_code": item.get("sku_code", ""),
            "batch_name": item.get("batch_name", ""),
            "expires_at": item.get("expires_at", ""),
        })
    return buf.getvalue()


def is_expired_text(value: str) -> bool:
    text_value = str(value or "").strip()
    if not text_value:
        return False
    try:
        normalized = text_value[:-1] + "+00:00" if text_value.endswith("Z") else text_value
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < utcnow()
    except Exception:
        return False


def _find_success_idempotent_event(session: Session, card_id: int, idempotency_key: str) -> DeliveryCardEventModel | None:
    key = str(idempotency_key or "").strip()
    if not key:
        return None
    return session.exec(
        select(DeliveryCardEventModel).where(
            DeliveryCardEventModel.card_id == card_id,
            DeliveryCardEventModel.idempotency_key == key,
            DeliveryCardEventModel.result == RESULT_SUCCESS,
        ).order_by(DeliveryCardEventModel.id.asc())
    ).first()


def _success_response(card: DeliveryCardModel, sku: DeliverySkuModel, payload: dict[str, Any], *, first_redeem: bool, idempotent_replay: bool = False, redeem_index: int | None = None) -> dict[str, Any]:
    index = int(redeem_index if redeem_index is not None else card.redeem_count or 0)
    return {
        "ok": True,
        "first_redeem": bool(first_redeem),
        "idempotent_replay": bool(idempotent_replay),
        "redeem_index": index,
        "redeem_count": int(card.redeem_count or index),
        "sku": {"code": sku.code, "name": sku.name},
        "card": {
            "status": card.status,
            "code_mask": card.code_mask,
            "assigned_at": card.assigned_at,
            "first_redeemed_at": card.first_redeemed_at,
            "last_redeemed_at": card.last_redeemed_at,
        },
        "account": payload.get("account") if isinstance(payload.get("account"), dict) else payload,
    }


def redeem_card(
    *,
    code: str,
    consumer: str = "",
    request_id: str = "",
    idempotency_key: str = "",
    authorization: str = "",
    client_ip: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    started = time.time()
    trace = trace_id()
    normalized = normalize_code(code)
    key = str(idempotency_key or request_id or "").strip()
    consumer_value = str(consumer or "").strip()[:120]
    base_log = {
        "trace": trace,
        "started_at": started,
        "request_id": request_id,
        "idempotency_key": key,
        "consumer": consumer_value,
        "api_token_id": "default",
        "client_ip": client_ip,
        "user_agent": user_agent,
        "code": normalized,
    }
    rate_identity = _rate_limit_identity(authorization=authorization, client_ip=client_ip, consumer=consumer_value)
    try:
        check_api_rate_limit(rate_identity)
        verify_api_token(authorization)
        if not normalized:
            raise DeliveryRedeemError(ERROR_INVALID_REQUEST, "卡密不能为空", action="failed")
    except DeliveryRedeemError as exc:
        record_api_failure(rate_identity, exc.code)
        write_failed_api_log(make_api_log_payload(**base_log, action=exc.action, result=RESULT_FAILED, error_code=exc.code, message=exc.message))
        return {"ok": False, "error_code": public_error_code(exc.code), "message": exc.message, "trace_id": trace}

    try:
        with Session(engine) as session:
            session.exec(text("BEGIN IMMEDIATE"))
            try:
                card = session.exec(select(DeliveryCardModel).where(DeliveryCardModel.code_hash == hash_code(normalized))).first()
                if not card:
                    raise DeliveryRedeemError(ERROR_CARD_NOT_AVAILABLE, "卡密不可用", action="failed")
                sku = session.exec(select(DeliverySkuModel).where(DeliverySkuModel.code == card.sku_code)).first()
                if not sku or not bool(sku.enabled):
                    raise DeliveryRedeemError(ERROR_SKU_DISABLED, "卡密类型已停用", action="failed", context={"card": card})
                if card.status == STATUS_DISABLED:
                    raise DeliveryRedeemError(ERROR_CARD_DISABLED, "卡密已禁用", action="failed", context={"card": card})
                if card.status == STATUS_BLOCKED:
                    raise DeliveryRedeemError(ERROR_CARD_NOT_AVAILABLE, "卡密已锁定", action="failed", context={"card": card})
                if card.status == STATUS_UNUSED and is_expired_text(card.expires_at):
                    card.status = STATUS_EXPIRED
                    card.updated_at = utcnow()
                    session.add(card)
                    raise DeliveryRedeemError(ERROR_CARD_EXPIRED, "卡密已过首次兑换有效期", action="failed", context={"card": card})

                if card.status == STATUS_REDEEMED:
                    existing_event = _find_success_idempotent_event(session, int(card.id or 0), key)
                    account = session.get(AccountModel, int(card.assigned_account_id or 0)) if int(card.assigned_account_id or 0) > 0 else None
                    payload = safe_json_loads(card.delivery_payload_json, {})
                    if not isinstance(payload, dict):
                        payload = {}
                    if account is not None:
                        # Self-heal historical drift: redeemed cards must leave the account marked used.
                        extra = account_extra(account)
                        if not bool(extra.get("manually_used")):
                            mark_account_assigned(account, card, card.assigned_at or iso_now())
                            session.add(account)
                    if existing_event:
                        response = _success_response(card, sku, payload, first_redeem=False, idempotent_replay=True, redeem_index=int(existing_event.delivery_sequence or card.redeem_count or 1))
                        write_api_log(session, make_api_log_payload(
                            **base_log,
                            card=card,
                            sku_code=sku.code,
                            account=account,
                            action="idempotent_replay",
                            result=RESULT_SUCCESS,
                            redeem_index=response["redeem_index"],
                            idempotent_replay=True,
                            decision={"steps": [{"name": "idempotency", "status": "passed", "message": "成功结果幂等重放"}]},
                            response_summary={"ok": True, "redeem_index": response["redeem_index"]},
                            message="成功结果幂等重放",
                        ))
                        session.commit()
                        record_api_success(rate_identity)
                        response["trace_id"] = trace
                        return response
                    if not bool(sku.allow_refetch):
                        raise DeliveryRedeemError(ERROR_REFETCH_LIMIT_EXCEEDED, "该卡密不允许重复取回", action="failed", context={"card": card})
                    if int(sku.max_refetch_count or 0) > 0 and int(card.redeem_count or 0) >= int(sku.max_refetch_count or 0):
                        raise DeliveryRedeemError(ERROR_REFETCH_LIMIT_EXCEEDED, "已超过最大取回次数", action="failed", context={"card": card})
                    card.redeem_count = int(card.redeem_count or 0) + 1
                    card.last_redeemed_at = iso_now()
                    card.last_redeem_ip = client_ip
                    card.last_consumer = consumer_value
                    card.updated_at = utcnow()
                    session.add(card)
                    event = DeliveryCardEventModel(
                        card_id=int(card.id or 0),
                        batch_id=int(card.batch_id or 0),
                        sku_code=card.sku_code,
                        account_id=int(card.assigned_account_id or 0),
                        event_type=EVENT_REFETCH,
                        result=RESULT_SUCCESS,
                        delivery_sequence=int(card.redeem_count or 0),
                        request_id=request_id,
                        idempotency_key=key,
                        consumer=consumer_value,
                        client_ip=client_ip,
                        user_agent=user_agent,
                        api_token_id="default",
                        response_profile=sku.delivery_profile,
                        message="重复取回已绑定账号",
                    )
                    session.add(event)
                    response = _success_response(card, sku, payload, first_redeem=False)
                    write_api_log(session, make_api_log_payload(
                        **base_log,
                        card=card,
                        sku_code=sku.code,
                        account=account,
                        action=EVENT_REFETCH,
                        result=RESULT_SUCCESS,
                        redeem_index=int(card.redeem_count or 0),
                        decision={"steps": [{"name": "refetch", "status": "passed", "message": "返回首次绑定账号快照"}]},
                        response_summary={"ok": True, "redeem_index": int(card.redeem_count or 0)},
                        message="重复取回已绑定账号",
                    ))
                    session.commit()
                    record_api_success(rate_identity)
                    response["trace_id"] = trace
                    return response

                if card.status != STATUS_UNUSED:
                    raise DeliveryRedeemError(ERROR_CARD_NOT_AVAILABLE, "卡密不可用", action="failed", context={"card": card})

                stock = stock_summary(session)
                account, candidate_count = pick_next_account_for_sku(session, sku)
                if account is None:
                    now = iso_now()
                    card.last_failure_code = ERROR_POOL_EMPTY
                    card.last_failure_at = now
                    card.updated_at = utcnow()
                    session.add(card)
                    event = DeliveryCardEventModel(
                        card_id=int(card.id or 0),
                        batch_id=int(card.batch_id or 0),
                        sku_code=card.sku_code,
                        event_type=EVENT_FAILED,
                        result=RESULT_FAILED,
                        failure_code=ERROR_POOL_EMPTY,
                        request_id=request_id,
                        idempotency_key=key,
                        consumer=consumer_value,
                        client_ip=client_ip,
                        user_agent=user_agent,
                        api_token_id="default",
                        message="对应类型账号库存不足",
                    )
                    session.add(event)
                    write_api_log(session, make_api_log_payload(
                        **base_log,
                        card=card,
                        sku_code=sku.code,
                        action="stock_empty",
                        result=RESULT_FAILED,
                        error_code=ERROR_POOL_EMPTY,
                        stock_before=stock,
                        decision={"steps": [{"name": "stock_pick", "status": "failed", "message": "对应类型账号库存不足"}]},
                        message="对应类型账号库存不足",
                    ))
                    session.commit()
                    record_api_failure(rate_identity, ERROR_POOL_EMPTY)
                    return {"ok": False, "error_code": ERROR_POOL_EMPTY, "message": f"当前 {sku.name} 库存不足，请稍后重试", "sku": {"code": sku.code, "name": sku.name}, "trace_id": trace}

                duplicate = assert_no_delivery_duplicate(session, account=account, current_card_id=int(card.id or 0))
                if not duplicate.ok:
                    write_api_log(session, make_api_log_payload(
                        **base_log,
                        card=card,
                        sku_code=sku.code,
                        account=account,
                        action="duplicate_check",
                        result=RESULT_FAILED,
                        error_code=ERROR_DUPLICATE_ACCOUNT_DETECTED,
                        duplicate=duplicate,
                        stock_before=stock,
                        decision={"steps": [{"name": "duplicate_check", "status": "failed", "message": duplicate.message}]},
                        message=duplicate.message,
                    ))
                    session.commit()
                    record_api_failure(rate_identity, ERROR_DUPLICATE_ACCOUNT_DETECTED)
                    return {"ok": False, "error_code": ERROR_DUPLICATE_ACCOUNT_DETECTED, "message": duplicate.message, "trace_id": trace}

                now = iso_now()
                payload = {"account": build_delivery_payload(account, sku), "profile": sku.delivery_profile, "sku_code": sku.code, "created_at": now}
                card.status = STATUS_REDEEMED
                card.assigned_account_id = int(account.id or 0)
                card.assigned_email_snapshot = account.email
                card.assigned_at = now
                card.redeem_count = 1
                card.first_redeemed_at = now
                card.last_redeemed_at = now
                card.first_redeem_ip = client_ip
                card.last_redeem_ip = client_ip
                card.first_consumer = consumer_value
                card.last_consumer = consumer_value
                card.delivery_payload_json = safe_json_dumps(payload)
                card.updated_at = utcnow()
                mark_account_assigned(account, card, now)
                session.add(card)
                session.add(account)
                event = DeliveryCardEventModel(
                    card_id=int(card.id or 0),
                    batch_id=int(card.batch_id or 0),
                    sku_code=card.sku_code,
                    account_id=int(account.id or 0),
                    event_type=EVENT_FIRST_REDEEM,
                    result=RESULT_SUCCESS,
                    delivery_sequence=1,
                    request_id=request_id,
                    idempotency_key=key,
                    consumer=consumer_value,
                    client_ip=client_ip,
                    user_agent=user_agent,
                    api_token_id="default",
                    response_profile=sku.delivery_profile,
                    message="首次兑换成功并绑定账号",
                    detail_json=safe_json_dumps({"candidate_count": candidate_count}),
                )
                session.add(event)
                response = _success_response(card, sku, payload, first_redeem=True)
                write_api_log(session, make_api_log_payload(
                    **base_log,
                    card=card,
                    sku_code=sku.code,
                    account=account,
                    action=EVENT_FIRST_REDEEM,
                    result=RESULT_SUCCESS,
                    redeem_index=1,
                    first_redeem=True,
                    duplicate=duplicate,
                    stock_before=stock,
                    decision={"steps": [
                        {"name": "card_lookup", "status": "passed", "message": f"匹配卡密 {card.code_mask}"},
                        {"name": "stock_pick", "status": "passed", "message": f"{sku.code.upper()} 候选账号 {candidate_count} 个，选中 {account.email}"},
                        {"name": "duplicate_check", "status": "passed", "message": duplicate.message},
                        {"name": "commit", "status": "passed", "message": "卡密绑定账号并标记已使用"},
                    ]},
                    response_summary={"ok": True, "redeem_index": 1, "email": account.email, "phone_bound": bool(payload.get("account", {}).get("phone_binding", {}).get("bound")), "phone": payload.get("account", {}).get("phone", "")},
                    message="首次兑换成功并绑定账号",
                ))
                session.commit()
                record_api_success(rate_identity)
                response["trace_id"] = trace
                return response
            except DeliveryRedeemError as exc:
                context_card = exc.context.get("card") if isinstance(exc.context, dict) else None
                session.rollback()
                record_api_failure(rate_identity, exc.code)
                write_failed_api_log(make_api_log_payload(
                    **base_log,
                    card=context_card if isinstance(context_card, DeliveryCardModel) else None,
                    action=exc.action,
                    result=RESULT_FAILED,
                    error_code=exc.code,
                    message=exc.message,
                ))
                return {"ok": False, "error_code": public_error_code(exc.code), "message": exc.message, "trace_id": trace}
    except Exception as exc:
        record_api_failure(rate_identity, ERROR_ACCOUNT_ALLOCATION_FAILED)
        write_failed_api_log(make_api_log_payload(**base_log, action="failed", result=RESULT_FAILED, error_code=ERROR_ACCOUNT_ALLOCATION_FAILED, message=str(exc)))
        return {"ok": False, "error_code": ERROR_ACCOUNT_ALLOCATION_FAILED, "message": f"兑换失败: {exc}", "trace_id": trace}


def list_batches(session: Session, sku_code: str = "") -> dict[str, Any]:
    stmt = select(DeliveryCardBatchModel).order_by(DeliveryCardBatchModel.id.desc())
    if sku_code:
        stmt = stmt.where(DeliveryCardBatchModel.sku_code == sku_code)
    rows = session.exec(stmt).all()
    return {"items": [serialize_batch(row, session) for row in rows], "total": len(rows)}


def list_cards(session: Session, *, sku_code: str = "", status: str = "", batch_id: int = 0, search: str = "", limit: int = 200) -> dict[str, Any]:
    stmt = select(DeliveryCardModel).order_by(DeliveryCardModel.id.desc())
    if sku_code:
        stmt = stmt.where(DeliveryCardModel.sku_code == sku_code)
    if status:
        stmt = stmt.where(DeliveryCardModel.status == status)
    if batch_id:
        stmt = stmt.where(DeliveryCardModel.batch_id == batch_id)
    rows = session.exec(stmt.limit(max(1, min(int(limit or 200), 1000)))).all()
    q = str(search or "").strip().lower()
    if q:
        rows = [row for row in rows if q in str(row.code_mask or "").lower() or q in str(row.assigned_email_snapshot or "").lower()]
    return {"items": [serialize_card(row) for row in rows], "total": len(rows)}


def card_detail(session: Session, card_id: int) -> dict[str, Any]:
    card = session.get(DeliveryCardModel, int(card_id or 0))
    if not card:
        raise HTTPException(404, "卡密不存在")
    account = session.get(AccountModel, int(card.assigned_account_id or 0)) if int(card.assigned_account_id or 0) > 0 else None
    events = session.exec(select(DeliveryCardEventModel).where(DeliveryCardEventModel.card_id == int(card.id or 0)).order_by(DeliveryCardEventModel.id.desc()).limit(50)).all()
    logs = session.exec(select(DeliveryRedeemApiLogModel).where(DeliveryRedeemApiLogModel.card_id == int(card.id or 0)).order_by(DeliveryRedeemApiLogModel.id.desc()).limit(50)).all()
    return {
        "card": serialize_card(card, include_payload=True),
        "account": row_to_dict(account) if account else None,
        "events": [serialize_event(event) for event in events],
        "api_logs": [serialize_api_log(log) for log in logs],
    }


def set_card_status(session: Session, card_id: int, status: str, reason: str = "") -> dict[str, Any]:
    card = session.get(DeliveryCardModel, int(card_id or 0))
    if not card:
        raise HTTPException(404, "卡密不存在")
    requested_status = str(status or "").strip()
    final_status = requested_status
    if requested_status == STATUS_UNUSED and int(card.assigned_account_id or 0) > 0:
        # 已经交付过的卡密重新启用时必须回到 redeemed，不能变成 unused，
        # 否则外部再次兑换会走首次分配路径，存在重复交付风险。
        final_status = STATUS_REDEEMED
    card.status = final_status
    if final_status == STATUS_DISABLED:
        card.disabled_reason = str(reason or "").strip()
    elif final_status != STATUS_DISABLED:
        card.disabled_reason = ""
    card.updated_at = utcnow()
    session.add(card)
    session.add(DeliveryCardEventModel(
        card_id=int(card.id or 0),
        batch_id=int(card.batch_id or 0),
        sku_code=card.sku_code,
        account_id=int(card.assigned_account_id or 0),
        event_type="admin_disable" if final_status == STATUS_DISABLED else "admin_enable",
        result=RESULT_SUCCESS,
        message=reason or ("启用卡密，已绑定账号恢复为已兑换" if final_status == STATUS_REDEEMED else "启用卡密"),
        detail_json=safe_json_dumps({"requested_status": requested_status, "final_status": final_status}),
    ))
    session.commit()
    session.refresh(card)
    return serialize_card(card)


def list_events(session: Session, *, sku_code: str = "", result: str = "", failure_code: str = "", consumer: str = "", request_id: str = "", limit: int = 200) -> dict[str, Any]:
    stmt = select(DeliveryCardEventModel).order_by(DeliveryCardEventModel.id.desc())
    if sku_code:
        stmt = stmt.where(DeliveryCardEventModel.sku_code == sku_code)
    if result:
        stmt = stmt.where(DeliveryCardEventModel.result == result)
    if failure_code:
        stmt = stmt.where(DeliveryCardEventModel.failure_code == failure_code)
    if consumer:
        stmt = stmt.where(DeliveryCardEventModel.consumer.contains(consumer))
    if request_id:
        stmt = stmt.where(DeliveryCardEventModel.request_id.contains(request_id))
    rows = session.exec(stmt.limit(max(1, min(int(limit or 200), 1000)))).all()
    return {"items": [serialize_event(row) for row in rows], "total": len(rows)}


def list_api_logs(session: Session, *, sku_code: str = "", result: str = "", error_code: str = "", consumer: str = "", request_id: str = "", limit: int = 200) -> dict[str, Any]:
    stmt = select(DeliveryRedeemApiLogModel).order_by(DeliveryRedeemApiLogModel.id.desc())
    if sku_code:
        stmt = stmt.where(DeliveryRedeemApiLogModel.sku_code == sku_code)
    if result:
        stmt = stmt.where(DeliveryRedeemApiLogModel.result == result)
    if error_code:
        stmt = stmt.where(DeliveryRedeemApiLogModel.error_code == error_code)
    if consumer:
        stmt = stmt.where(DeliveryRedeemApiLogModel.consumer.contains(consumer))
    if request_id:
        stmt = stmt.where(DeliveryRedeemApiLogModel.request_id.contains(request_id))
    rows = session.exec(stmt.limit(max(1, min(int(limit or 200), 1000)))).all()
    return {"items": [serialize_api_log(row) for row in rows], "total": len(rows)}


def admin_summary(session: Session) -> dict[str, Any]:
    today_prefix = utcnow().strftime("%Y-%m-%d")
    logs = session.exec(select(DeliveryRedeemApiLogModel)).all()
    today_logs = [log for log in logs if str(log.created_at.isoformat() if log.created_at else "").startswith(today_prefix)]
    recent_errors = [serialize_api_log(log) for log in sorted([log for log in logs if log.result == RESULT_FAILED], key=lambda item: int(item.id or 0), reverse=True)[:10]]
    return {
        "api": {
            **get_delivery_settings(),
            "last_called_at": logs[-1].created_at.isoformat() if logs else "",
            "today_success": sum(1 for log in today_logs if log.result == RESULT_SUCCESS),
            "today_failed": sum(1 for log in today_logs if log.result == RESULT_FAILED),
            "duplicate_failed": sum(1 for log in today_logs if log.duplicate_check_status == "failed"),
            "pool_empty": sum(1 for log in today_logs if log.error_code == ERROR_POOL_EMPTY),
        },
        "stock": stock_summary(session),
        "recent_errors": recent_errors,
    }
