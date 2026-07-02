from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

from sqlmodel import Session, select

from core import db as core_db
from core.db import AccountModel


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_bound_phone(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) < 8:
        return ""
    if text.startswith("00") and len(digits) > 2:
        digits = digits[2:]
    return f"+{digits}"


def _safe_text(value: Any, max_len: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) > max_len:
        return text[:max_len]
    return text


def chatgpt_bound_phone_payload(extra: dict[str, Any] | None) -> dict[str, Any]:
    extra = extra if isinstance(extra, dict) else {}
    payload = extra.get("chatgpt_bound_phone") if isinstance(extra.get("chatgpt_bound_phone"), dict) else {}
    phone = normalize_bound_phone(
        payload.get("phone")
        or payload.get("phone_number")
        or extra.get("chatgpt_bound_phone_number")
    )
    masked = _safe_text(
        payload.get("masked")
        or payload.get("masked_phone")
        or extra.get("chatgpt_bound_phone_masked")
    )
    if not phone and not masked:
        return {}
    return {
        "phone": phone,
        "phone_number": phone,
        "masked": masked,
        "masked_phone": masked,
        "source": _safe_text(payload.get("source")),
        "detected_at": _safe_text(payload.get("detected_at")),
        "updated_at": _safe_text(payload.get("updated_at") or payload.get("detected_at")),
        "last_seen_reason": _safe_text(payload.get("last_seen_reason")),
        "verification_status": _safe_text(payload.get("verification_status") or "required"),
        "display": phone or masked,
        "is_masked": bool(masked and not phone),
    }


def chatgpt_phone_challenge_payload(extra: dict[str, Any] | None) -> dict[str, Any]:
    extra = extra if isinstance(extra, dict) else {}
    payload = extra.get("chatgpt_phone_challenge") if isinstance(extra.get("chatgpt_phone_challenge"), dict) else {}
    challenge_type = _safe_text(payload.get("type") or payload.get("challenge_type"))
    status = _safe_text(payload.get("status") or payload.get("verification_status"))
    if not challenge_type and not status:
        return {}
    phone = normalize_bound_phone(payload.get("phone") or payload.get("phone_number"))
    masked = _safe_text(payload.get("masked") or payload.get("masked_phone"))
    display = _safe_text(payload.get("display")) or phone or masked
    if not display and challenge_type == "add_phone":
        display = "未绑定手机号"
    return {
        "type": challenge_type,
        "challenge_type": challenge_type,
        "status": status,
        "phone": phone,
        "phone_number": phone,
        "masked": masked,
        "masked_phone": masked,
        "source": _safe_text(payload.get("source")),
        "message": _safe_text(payload.get("message")),
        "seen_at": _safe_text(payload.get("seen_at") or payload.get("detected_at")),
        "updated_at": _safe_text(payload.get("updated_at") or payload.get("seen_at") or payload.get("detected_at")),
        "allow_add_phone_verification": bool(payload.get("allow_add_phone_verification")),
        "allow_existing_phone_verification": bool(payload.get("allow_existing_phone_verification")),
        "display": display,
    }


def _find_account(session: Session, *, account_id: int = 0, email: str = "") -> AccountModel | None:
    account_id = int(account_id or 0)
    if account_id > 0:
        account = session.get(AccountModel, account_id)
        if account is not None and account.platform == "chatgpt":
            return account
    target_email = str(email or "").strip().lower()
    if not target_email:
        return None
    rows = session.exec(
        select(AccountModel)
        .where(AccountModel.platform == "chatgpt")
        .order_by(AccountModel.updated_at.desc())
    ).all()
    for row in rows:
        if str(row.email or "").strip().lower() == target_email:
            return row
    return None


def _history_entry(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "phone": normalize_bound_phone(payload.get("phone") or payload.get("phone_number")),
        "masked": _safe_text(payload.get("masked") or payload.get("masked_phone")),
        "source": _safe_text(payload.get("source")),
        "detected_at": _safe_text(payload.get("detected_at") or payload.get("updated_at")),
        "last_seen_reason": _safe_text(payload.get("last_seen_reason")),
        "verification_status": _safe_text(payload.get("verification_status")),
    }


def upsert_chatgpt_bound_phone(
    *,
    account_id: int = 0,
    email: str = "",
    phone: Any = "",
    masked: Any = "",
    source: str = "",
    reason: str = "existing_phone_otp",
    verification_status: str = "required",
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Persist the OpenAI already-bound phone observed during existing-phone OTP.

    Full phone wins over masked. Masked observations never overwrite an existing
    full phone, but they are retained as last_masked_seen for debugging.
    """
    normalized_phone = normalize_bound_phone(phone)
    masked_text = _safe_text(masked)
    if not normalized_phone and not masked_text:
        return {"updated": False, "reason": "empty_phone_hint"}

    try:
        with Session(core_db.engine) as session:
            account = _find_account(session, account_id=account_id, email=email)
            if account is None:
                return {"updated": False, "reason": "account_not_found"}

            try:
                extra = account.get_extra()
            except Exception:
                extra = {}
            if not isinstance(extra, dict):
                extra = {}

            now = _utcnow_iso()
            current = extra.get("chatgpt_bound_phone") if isinstance(extra.get("chatgpt_bound_phone"), dict) else {}
            current_phone = normalize_bound_phone(current.get("phone") or current.get("phone_number"))
            current_masked = _safe_text(current.get("masked") or current.get("masked_phone"))
            history = extra.get("chatgpt_bound_phone_history") if isinstance(extra.get("chatgpt_bound_phone_history"), list) else []
            history = [dict(item) for item in history if isinstance(item, dict)]

            new_payload = dict(current or {})
            should_replace_main = False
            if normalized_phone:
                should_replace_main = True
                if current and (current_phone != normalized_phone or (not current_phone and current_masked and current_masked != masked_text)):
                    old_entry = _history_entry(current)
                    if old_entry.get("phone") or old_entry.get("masked"):
                        history.append(old_entry)
                new_payload.update(
                    {
                        "phone": normalized_phone,
                        "phone_number": normalized_phone,
                        "masked": masked_text,
                        "masked_phone": masked_text,
                    }
                )
            elif masked_text and not current_phone:
                should_replace_main = True
                if current and current_masked and current_masked != masked_text:
                    history.append(_history_entry(current))
                new_payload.update(
                    {
                        "phone": "",
                        "phone_number": "",
                        "masked": masked_text,
                        "masked_phone": masked_text,
                    }
                )
            elif masked_text and current_phone:
                new_payload["last_masked_seen"] = masked_text
                new_payload["last_masked_seen_at"] = now

            if not should_replace_main and not (masked_text and current_phone):
                return {"updated": False, "reason": "masked_did_not_overwrite_full_phone"}

            new_payload.setdefault("detected_at", now)
            new_payload.update(
                {
                    "source": _safe_text(source) or _safe_text(new_payload.get("source")),
                    "updated_at": now,
                    "last_seen_reason": _safe_text(reason) or "existing_phone_otp",
                    "verification_status": _safe_text(verification_status) or "required",
                }
            )
            extra["chatgpt_bound_phone"] = new_payload
            extra["chatgpt_bound_phone_history"] = history[-5:]
            # Flat aliases make ad-hoc exports/searches easier without changing DB schema.
            if normalize_bound_phone(new_payload.get("phone")):
                extra["chatgpt_bound_phone_number"] = normalize_bound_phone(new_payload.get("phone"))
            elif not extra.get("chatgpt_bound_phone_number"):
                extra["chatgpt_bound_phone_masked"] = _safe_text(new_payload.get("masked"))

            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()

            display = normalize_bound_phone(new_payload.get("phone")) or _safe_text(new_payload.get("masked"))
            if callable(log_fn) and display:
                log_fn(f"[手机号验证] 已记录账号绑定手机号: account_id={account.id} phone={display}")
            return {
                "updated": True,
                "account_id": int(account.id or 0),
                "email": str(account.email or ""),
                "bound_phone": chatgpt_bound_phone_payload(extra),
            }
    except Exception as exc:
        if callable(log_fn):
            log_fn(f"[手机号验证] 记录绑定手机号失败: {exc}")
        return {"updated": False, "reason": str(exc or "persist_failed")}


def upsert_chatgpt_phone_challenge(
    *,
    account_id: int = 0,
    email: str = "",
    challenge_type: str = "",
    status: str = "",
    phone: Any = "",
    masked: Any = "",
    source: str = "",
    message: str = "",
    allow_add_phone_verification: bool | None = None,
    allow_existing_phone_verification: bool | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    challenge_type = _safe_text(challenge_type)
    status = _safe_text(status)
    normalized_phone = normalize_bound_phone(phone)
    masked_text = _safe_text(masked)
    if not challenge_type and not status:
        return {"updated": False, "reason": "empty_challenge"}

    try:
        with Session(core_db.engine) as session:
            account = _find_account(session, account_id=account_id, email=email)
            if account is None:
                return {"updated": False, "reason": "account_not_found"}

            try:
                extra = account.get_extra()
            except Exception:
                extra = {}
            if not isinstance(extra, dict):
                extra = {}

            now = _utcnow_iso()
            payload = {
                "type": challenge_type,
                "challenge_type": challenge_type,
                "status": status,
                "phone": normalized_phone,
                "phone_number": normalized_phone,
                "masked": masked_text,
                "masked_phone": masked_text,
                "source": _safe_text(source),
                "message": _safe_text(message),
                "seen_at": now,
                "updated_at": now,
                "display": normalized_phone or masked_text or ("未绑定手机号" if challenge_type == "add_phone" else ""),
            }
            if allow_add_phone_verification is not None:
                payload["allow_add_phone_verification"] = bool(allow_add_phone_verification)
            if allow_existing_phone_verification is not None:
                payload["allow_existing_phone_verification"] = bool(allow_existing_phone_verification)

            history = extra.get("chatgpt_phone_challenge_history") if isinstance(extra.get("chatgpt_phone_challenge_history"), list) else []
            history = [dict(item) for item in history if isinstance(item, dict)]
            current = extra.get("chatgpt_phone_challenge") if isinstance(extra.get("chatgpt_phone_challenge"), dict) else {}
            if current:
                history.append(dict(current))
            extra["chatgpt_phone_challenge"] = payload
            extra["chatgpt_phone_challenge_history"] = history[-5:]
            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()

            if callable(log_fn):
                label = payload["display"] or payload["status"] or payload["type"]
                log_fn(f"[手机号验证] 已记录手机号挑战: account_id={account.id} {label}")
            return {
                "updated": True,
                "account_id": int(account.id or 0),
                "email": str(account.email or ""),
                "phone_challenge": chatgpt_phone_challenge_payload(extra),
            }
    except Exception as exc:
        if callable(log_fn):
            log_fn(f"[手机号验证] 记录手机号挑战失败: {exc}")
        return {"updated": False, "reason": str(exc or "persist_failed")}
