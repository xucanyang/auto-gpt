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
        binding = extra.get("chatgpt_phone_binding") if isinstance(extra.get("chatgpt_phone_binding"), dict) else {}
        if isinstance(binding, dict) and str(binding.get("status") or "") == "bound":
            phone = normalize_bound_phone(binding.get("phone") or binding.get("phone_number"))
            masked = _safe_text(binding.get("masked") or binding.get("masked_phone"))
            if phone or masked:
                return {
                    "phone": phone,
                    "phone_number": phone,
                    "masked": masked,
                    "masked_phone": masked,
                    "api_url": _safe_text(binding.get("api_url")),
                    "source_api_url": _safe_text(binding.get("source_api_url") or binding.get("api_url")),
                    "source": _safe_text(binding.get("source") or "phone_binding_test"),
                    "updated_at": _safe_text(binding.get("bound_at") or binding.get("code_time") or _utcnow_iso()),
                    "verification_status": "verified",
                    "display": phone or masked,
                    "is_masked": bool(masked and not phone),
                }
        return {}
    binding_api_url = _safe_text((extra.get("chatgpt_phone_binding") if isinstance(extra.get("chatgpt_phone_binding"), dict) else {}).get("api_url"))
    binding_source_api_url = _safe_text((extra.get("chatgpt_phone_binding") if isinstance(extra.get("chatgpt_phone_binding"), dict) else {}).get("source_api_url"))
    return {
        "phone": phone,
        "phone_number": phone,
        "masked": masked,
        "masked_phone": masked,
        "api_url": _safe_text(payload.get("api_url") or binding_api_url),
        "source_api_url": _safe_text(payload.get("source_api_url") or binding_source_api_url or payload.get("api_url") or binding_api_url),
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


def _build_confirmed_phone_binding_payload(
    *,
    account_id: int = 0,
    email: str = "",
    phone: Any = "",
    api_url: Any = "",
    source_api_url: Any = "",
    raw_line: Any = "",
    task_id: Any = "",
    source: Any = "",
    flow: Any = "",
    bound_at: Any = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the durable payload emitted only after OpenAI accepted a phone OTP."""
    normalized_phone = normalize_bound_phone(phone)
    if not normalized_phone:
        return {}, {}

    now = _safe_text(bound_at, max_len=80) or _utcnow_iso()
    api_url_text = _safe_text(api_url, max_len=4096)
    source_api_url_text = _safe_text(source_api_url, max_len=4096)
    if not source_api_url_text and api_url_text:
        source_api_url_text = api_url_text
    source_text = _safe_text(source, max_len=120) or "oauth_add_phone"
    binding = {
        "phone": normalized_phone,
        "phone_number": normalized_phone,
        "api_url": api_url_text,
        "source_api_url": source_api_url_text,
        "raw_line": _safe_text(raw_line, max_len=4096),
        "account_id": int(account_id or 0),
        "email": _safe_text(email, max_len=320),
        "task_id": _safe_text(task_id, max_len=240),
        "source": source_text,
        "flow": _safe_text(flow, max_len=120) or "add_phone",
        "status": "bound",
        "status_label": "手机号验证成功",
        "verification_status": "verified",
        "bound_at": now,
        "detected_at": now,
        "updated_at": now,
    }
    bound_phone = {
        "phone": normalized_phone,
        "phone_number": normalized_phone,
        "masked": "",
        "masked_phone": "",
        "api_url": api_url_text,
        "source_api_url": source_api_url_text,
        "source": source_text,
        "detected_at": now,
        "updated_at": now,
        "last_seen_reason": "add_phone_otp_validated",
        "verification_status": "verified",
        "status": "bound",
        "display": normalized_phone,
        "is_masked": False,
    }
    return binding, bound_phone


def record_chatgpt_confirmed_phone_binding(
    *,
    account_id: int = 0,
    email: str = "",
    phone: Any = "",
    api_url: Any = "",
    source_api_url: Any = "",
    raw_line: Any = "",
    task_id: Any = "",
    source: Any = "",
    flow: Any = "",
    bound_at: Any = "",
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Persist a phone binding that OpenAI has explicitly confirmed by OTP.

    ``chatgpt_bound_phone`` remains useful for passive existing-phone
    observations. This helper is deliberately narrower: callers must invoke it
    only after ``phone-otp/validate`` succeeds, so the binding can safely drive
    downstream delivery classification.
    """
    binding, bound_phone = _build_confirmed_phone_binding_payload(
        account_id=account_id,
        email=email,
        phone=phone,
        api_url=api_url,
        source_api_url=source_api_url,
        raw_line=raw_line,
        task_id=task_id,
        source=source,
        flow=flow,
        bound_at=bound_at,
    )
    if not binding:
        return {"updated": False, "reason": "empty_phone"}

    try:
        with Session(core_db.engine) as session:
            account = _find_account(session, account_id=account_id, email=email)
            if account is None:
                # New registrations do not have a local account row yet. The
                # caller carries this payload through RegistrationResult.metadata.
                return {
                    "updated": False,
                    "reason": "account_not_found",
                    "phone_binding": binding,
                    "bound_phone": bound_phone,
                }

            try:
                extra = account.get_extra()
            except Exception:
                extra = {}
            if not isinstance(extra, dict):
                extra = {}

            binding["account_id"] = int(account.id or 0)
            binding["email"] = str(account.email or "")

            binding_history = extra.get("chatgpt_phone_binding_history")
            if not isinstance(binding_history, list):
                binding_history = []
            binding_history = [dict(item) for item in binding_history if isinstance(item, dict)]
            binding_history.append(dict(binding))

            existing_bound_phone = (
                extra.get("chatgpt_bound_phone")
                if isinstance(extra.get("chatgpt_bound_phone"), dict)
                else {}
            )
            bound_phone_history = extra.get("chatgpt_bound_phone_history")
            if not isinstance(bound_phone_history, list):
                bound_phone_history = []
            bound_phone_history = [dict(item) for item in bound_phone_history if isinstance(item, dict)]
            old_phone = normalize_bound_phone(
                existing_bound_phone.get("phone") or existing_bound_phone.get("phone_number")
            )
            if existing_bound_phone and old_phone and old_phone != binding["phone"]:
                old_entry = _history_entry(existing_bound_phone)
                if old_entry.get("phone") or old_entry.get("masked"):
                    bound_phone_history.append(old_entry)

            extra["chatgpt_phone_binding"] = binding
            extra["chatgpt_phone_binding_history"] = binding_history[-20:]
            extra["chatgpt_bound_phone"] = bound_phone
            extra["chatgpt_bound_phone_history"] = bound_phone_history[-5:]
            extra["chatgpt_bound_phone_number"] = binding["phone"]

            challenge = extra.get("chatgpt_phone_challenge")
            if isinstance(challenge, dict) and str(
                challenge.get("type") or challenge.get("challenge_type") or ""
            ).strip() == "add_phone":
                challenge_history = extra.get("chatgpt_phone_challenge_history")
                if not isinstance(challenge_history, list):
                    challenge_history = []
                challenge_history = [dict(item) for item in challenge_history if isinstance(item, dict)]
                challenge_history.append(dict(challenge))
                resolved_challenge = {
                    "type": "add_phone",
                    "challenge_type": "add_phone",
                    "status": "bound",
                    "phone": binding["phone"],
                    "phone_number": binding["phone"],
                    "masked": "",
                    "masked_phone": "",
                    "source": binding["source"],
                    "message": "OpenAI 手机号 OTP 验证成功",
                    "seen_at": binding["bound_at"],
                    "updated_at": binding["bound_at"],
                    "display": binding["phone"],
                }
                extra["chatgpt_phone_challenge"] = resolved_challenge
                extra["chatgpt_phone_challenge_history"] = challenge_history[-5:]

            account.set_extra(extra)
            account.updated_at = datetime.now(timezone.utc)
            session.add(account)
            session.commit()

            if callable(log_fn):
                log_fn(f"[手机号验证] 已确认并保存手机号绑定: account_id={account.id}")
            return {
                "updated": True,
                "account_id": int(account.id or 0),
                "email": str(account.email or ""),
                "phone_binding": binding,
                "bound_phone": bound_phone,
            }
    except Exception as exc:
        if callable(log_fn):
            log_fn(f"[手机号验证] 保存已确认手机号绑定失败: {exc}")
        return {
            "updated": False,
            "reason": str(exc or "persist_failed"),
            "phone_binding": binding,
            "bound_phone": bound_phone,
        }


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
