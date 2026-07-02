"""BaxiGPT 卡密池仓储。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from sqlmodel import Session, select

from core.db import AccountModel, BaxiGptCdkPoolModel, engine
from services.chatgpt_account_state import mark_payment_failed, mark_payment_succeeded

STATUS_AVAILABLE = "available"
STATUS_RESERVED = "reserved"
STATUS_SUBMITTED = "submitted"
STATUS_PROCESSING = "processing"
STATUS_PAID = "paid"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"

ALL_STATUSES = {
    STATUS_AVAILABLE,
    STATUS_RESERVED,
    STATUS_SUBMITTED,
    STATUS_PROCESSING,
    STATUS_PAID,
    STATUS_FAILED,
    STATUS_DISABLED,
}
TERMINAL_STATUSES = {STATUS_PAID, STATUS_FAILED, STATUS_DISABLED}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_text() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def normalize_code(value: Any) -> str:
    return str(value or "").strip()


def hash_code(value: Any) -> str:
    code = normalize_code(value)
    return hashlib.sha256(code.encode("utf-8")).hexdigest() if code else ""


def mask_code(value: Any) -> str:
    code = normalize_code(value)
    if not code:
        return ""
    if len(code) <= 8:
        if len(code) <= 2:
            return "*" * len(code)
        return f"{code[:1]}{'*' * max(len(code) - 2, 1)}{code[-1:]}"
    return f"{code[:4]}{'*' * max(len(code) - 8, 4)}{code[-4:]}"


def safe_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return json.dumps(parsed, ensure_ascii=False)
        except Exception:
            return json.dumps({"text": value[:1000]}, ensure_ascii=False)
    try:
        return json.dumps(value if isinstance(value, (dict, list)) else {}, ensure_ascii=False)
    except Exception:
        return "{}"


def parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def response_message(response: dict[str, Any] | None, fallback: str = "") -> str:
    data = response if isinstance(response, dict) else {}
    for key in ("message", "msg", "error", "detail", "reason"):
        text = str(data.get(key) or "").strip()
        if text:
            return text
    return fallback


def _response_int(response: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(response.get(key))
    except Exception:
        return int(default or 0)


def _clear_available_binding(model: BaxiGptCdkPoolModel) -> None:
    model.bound_account_id = 0
    model.bound_account_email = ""
    model.bound_at = ""
    model.task_id = ""
    model.order_id = ""
    model.display_id = ""
    model.remote_email = ""
    model.upstream_status = ""


def _parse_import_lines(raw: str) -> tuple[list[tuple[int, str, str, str]], list[dict[str, Any]]]:
    entries: list[tuple[int, str, str, str]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_line in enumerate(str(raw or "").splitlines(), start=1):
        line = str(raw_line or "").strip()
        if not line or line.startswith("#"):
            continue
        code_part = line
        label = ""
        if "----" in line:
            code_part, label = line.split("----", 1)
        code = normalize_code(code_part)
        if not code:
            errors.append({"line": index, "raw": line, "reason": "卡密为空"})
            continue
        code_hash = hash_code(code)
        if code_hash in seen:
            errors.append({"line": index, "raw": mask_code(code), "reason": "卡密重复，本次只保留第一次"})
            continue
        seen.add(code_hash)
        entries.append((index, code, str(label or "").strip(), line))
    return entries, errors


@dataclass(slots=True)
class BaxiGptCdkRecord:
    id: int
    code_value: str
    code_hash: str
    code_masked: str
    label: str
    status: str
    bound_account_id: int
    bound_account_email: str
    bound_at: str
    task_id: str
    order_id: str
    display_id: str
    remote_email: str
    upstream_status: str
    code_info_remaining: int
    code_info_total: int
    submit_response: dict[str, Any]
    last_status_response: dict[str, Any]
    last_query_response: dict[str, Any]
    last_error_code: str
    last_error_message: str
    submitted_at: str
    paid_at: str
    last_checked_at: str
    created_at: datetime | None
    updated_at: datetime | None

    @property
    def available(self) -> bool:
        return self.status == STATUS_AVAILABLE

    def to_dict(self, *, include_code: bool = False) -> dict[str, Any]:
        def iso(dt: datetime | None) -> str | None:
            return dt.isoformat() if dt else None

        payload = {
            "id": self.id,
            "code_hash": self.code_hash,
            "code_masked": self.code_masked,
            "label": self.label,
            "status": self.status,
            "available": self.available,
            "bound_account_id": self.bound_account_id,
            "bound_account_email": self.bound_account_email,
            "bound_at": self.bound_at or None,
            "task_id": self.task_id,
            "order_id": self.order_id,
            "display_id": self.display_id,
            "remote_email": self.remote_email,
            "upstream_status": self.upstream_status,
            "code_info_remaining": self.code_info_remaining,
            "code_info_total": self.code_info_total,
            "submit_response": dict(self.submit_response),
            "last_status_response": dict(self.last_status_response),
            "last_query_response": dict(self.last_query_response),
            "last_error_code": self.last_error_code,
            "last_error_message": self.last_error_message,
            "submitted_at": self.submitted_at or None,
            "paid_at": self.paid_at or None,
            "last_checked_at": self.last_checked_at or None,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }
        if include_code:
            payload["code_value"] = self.code_value
        return payload


def _to_record(model: BaxiGptCdkPoolModel) -> BaxiGptCdkRecord:
    return BaxiGptCdkRecord(
        id=int(model.id or 0),
        code_value=str(model.code_value or ""),
        code_hash=str(model.code_hash or ""),
        code_masked=str(model.code_masked or mask_code(model.code_value)),
        label=str(model.label or ""),
        status=str(model.status or STATUS_AVAILABLE),
        bound_account_id=int(model.bound_account_id or 0),
        bound_account_email=str(model.bound_account_email or ""),
        bound_at=str(model.bound_at or ""),
        task_id=str(model.task_id or ""),
        order_id=str(model.order_id or ""),
        display_id=str(model.display_id or ""),
        remote_email=str(model.remote_email or ""),
        upstream_status=str(model.upstream_status or ""),
        code_info_remaining=int(model.code_info_remaining or 0),
        code_info_total=int(model.code_info_total or 0),
        submit_response=parse_json_object(model.submit_response_json),
        last_status_response=parse_json_object(model.last_status_response_json),
        last_query_response=parse_json_object(model.last_query_response_json),
        last_error_code=str(model.last_error_code or ""),
        last_error_message=str(model.last_error_message or ""),
        submitted_at=str(model.submitted_at or ""),
        paid_at=str(model.paid_at or ""),
        last_checked_at=str(model.last_checked_at or ""),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class BaxiGptCdkRepository:
    @staticmethod
    def summarize(records: list[BaxiGptCdkRecord]) -> dict[str, Any]:
        by_status = {status: 0 for status in ALL_STATUSES}
        for record in records:
            status = str(record.status or STATUS_AVAILABLE)
            by_status[status] = by_status.get(status, 0) + 1
        return {
            "total": len(records),
            "available": by_status.get(STATUS_AVAILABLE, 0),
            "reserved": by_status.get(STATUS_RESERVED, 0),
            "submitted": by_status.get(STATUS_SUBMITTED, 0),
            "processing": by_status.get(STATUS_PROCESSING, 0),
            "paid": by_status.get(STATUS_PAID, 0),
            "failed": by_status.get(STATUS_FAILED, 0),
            "disabled": by_status.get(STATUS_DISABLED, 0),
        }

    def list(self, *, status: str = "", search: str = "") -> list[BaxiGptCdkRecord]:
        with Session(engine) as session:
            stmt = select(BaxiGptCdkPoolModel)
            if status:
                stmt = stmt.where(BaxiGptCdkPoolModel.status == status)
            stmt = stmt.order_by(BaxiGptCdkPoolModel.id)
            items = session.exec(stmt).all()
        query = str(search or "").strip().lower()
        records = [_to_record(item) for item in items]
        if query:
            records = [
                item for item in records
                if query in item.code_value.lower()
                or query in item.code_masked.lower()
                or query in item.bound_account_email.lower()
                or query in item.order_id.lower()
                or query in item.display_id.lower()
                or query in item.label.lower()
            ]
        return records

    def get_by_id(self, record_id: int) -> BaxiGptCdkRecord | None:
        with Session(engine) as session:
            model = session.get(BaxiGptCdkPoolModel, int(record_id or 0))
        return _to_record(model) if model else None

    def add(self, *, code: str, label: str = "") -> BaxiGptCdkRecord | None:
        code_value = normalize_code(code)
        if not code_value:
            return None
        code_hash = hash_code(code_value)
        now = _utcnow()
        with Session(engine) as session:
            existing = session.exec(
                select(BaxiGptCdkPoolModel).where(BaxiGptCdkPoolModel.code_hash == code_hash)
            ).first()
            if existing is None:
                model = BaxiGptCdkPoolModel(
                    code_value=code_value,
                    code_hash=code_hash,
                    code_masked=mask_code(code_value),
                    label=str(label or "").strip(),
                    status=STATUS_AVAILABLE,
                    created_at=now,
                    updated_at=now,
                )
                session.add(model)
                session.commit()
                session.refresh(model)
                return _to_record(model)
            if label:
                existing.label = str(label or "").strip()
            if existing.code_value != code_value:
                existing.code_value = code_value
                existing.code_masked = mask_code(code_value)
            existing.updated_at = now
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return _to_record(existing)

    def import_lines(self, raw: str) -> dict[str, Any]:
        entries, parse_errors = _parse_import_lines(raw)
        added = updated = skipped = 0
        items: list[dict[str, Any]] = []
        for _line_no, code, label, _raw_line in entries:
            before = self.get_by_hash(hash_code(code))
            record = self.add(code=code, label=label)
            if record is None:
                skipped += 1
                continue
            if before is None:
                added += 1
            else:
                updated += 1
            items.append(record.to_dict())
        return {"added": added, "updated": updated, "skipped": skipped, "errors": parse_errors, "items": items}

    def get_by_hash(self, code_hash: str) -> BaxiGptCdkRecord | None:
        value = str(code_hash or "").strip()
        if not value:
            return None
        with Session(engine) as session:
            model = session.exec(
                select(BaxiGptCdkPoolModel).where(BaxiGptCdkPoolModel.code_hash == value)
            ).first()
        return _to_record(model) if model else None

    def list_available(self, *, ids: list[int] | None = None) -> list[BaxiGptCdkRecord]:
        with Session(engine) as session:
            stmt = select(BaxiGptCdkPoolModel).where(BaxiGptCdkPoolModel.status == STATUS_AVAILABLE)
            if ids:
                stmt = stmt.where(BaxiGptCdkPoolModel.id.in_([int(value) for value in ids]))
            stmt = stmt.order_by(BaxiGptCdkPoolModel.updated_at, BaxiGptCdkPoolModel.id)
            items = session.exec(stmt).all()
        return [_to_record(item) for item in items]

    def list_by_ids(self, ids: list[int]) -> list[BaxiGptCdkRecord]:
        normalized_ids = [int(value) for value in ids if int(value or 0) > 0]
        if not normalized_ids:
            return []
        order = {record_id: index for index, record_id in enumerate(normalized_ids)}
        with Session(engine) as session:
            stmt = select(BaxiGptCdkPoolModel).where(BaxiGptCdkPoolModel.id.in_(normalized_ids))
            items = session.exec(stmt).all()
        records = [_to_record(item) for item in items]
        records.sort(key=lambda item: order.get(int(item.id or 0), len(order)))
        return records

    def set_status(self, record_id: int, status: str) -> BaxiGptCdkRecord | None:
        target = str(status or "").strip()
        if target not in ALL_STATUSES:
            return None
        with Session(engine) as session:
            model = session.get(BaxiGptCdkPoolModel, int(record_id or 0))
            if model is None:
                return None
            model.status = target
            if target == STATUS_AVAILABLE:
                _clear_available_binding(model)
                model.last_error_code = ""
                model.last_error_message = ""
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def delete(self, record_id: int) -> bool:
        with Session(engine) as session:
            model = session.get(BaxiGptCdkPoolModel, int(record_id or 0))
            if model is None:
                return False
            session.delete(model)
            session.commit()
            return True

    def reserve_for_account(self, record_id: int, *, account_id: int, email: str, task_id: str) -> BaxiGptCdkRecord | None:
        now_text = _now_text()
        with Session(engine) as session:
            model = session.get(BaxiGptCdkPoolModel, int(record_id or 0))
            if model is None or str(model.status or "") not in {STATUS_AVAILABLE, STATUS_RESERVED, STATUS_PROCESSING, STATUS_SUBMITTED}:
                return None
            model.status = STATUS_RESERVED
            model.bound_account_id = int(account_id or 0)
            model.bound_account_email = str(email or "").strip()
            model.bound_at = now_text
            model.task_id = str(task_id or "").strip()
            model.last_error_code = ""
            model.last_error_message = ""
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def mark_code_info(self, record_id: int, response: dict[str, Any]) -> BaxiGptCdkRecord | None:
        ok = bool(response.get("ok")) if isinstance(response, dict) else False
        remaining_present = isinstance(response, dict) and "remaining" in response
        remaining_value = _response_int(response, "remaining", 0) if isinstance(response, dict) else 0
        total_value = _response_int(response, "total", 0) if isinstance(response, dict) else 0
        message = response_message(response, "卡密不可用")
        with Session(engine) as session:
            model = session.get(BaxiGptCdkPoolModel, int(record_id or 0))
            if model is None:
                return None
            model.code_info_remaining = remaining_value
            model.code_info_total = total_value
            model.last_checked_at = _now_text()
            current_status = str(model.status or STATUS_AVAILABLE)
            if ok and (not remaining_present or remaining_value > 0):
                if current_status not in {STATUS_RESERVED, STATUS_PROCESSING, STATUS_SUBMITTED, STATUS_DISABLED}:
                    model.status = STATUS_AVAILABLE
                    _clear_available_binding(model)
                model.last_error_code = ""
                model.last_error_message = ""
            elif not ok or (remaining_present and remaining_value <= 0):
                if current_status not in {STATUS_PAID, STATUS_PROCESSING, STATUS_SUBMITTED, STATUS_DISABLED}:
                    model.status = STATUS_FAILED
                model.last_error_code = "code_info_failed"
                model.last_error_message = message[:1000]
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def mark_submit_success(self, record_id: int, response: dict[str, Any]) -> BaxiGptCdkRecord | None:
        raw_status = str(response.get("status") or STATUS_SUBMITTED).strip().lower() or STATUS_SUBMITTED
        status = STATUS_PROCESSING if raw_status in {"processing", "pending"} else STATUS_SUBMITTED
        if raw_status in {"paid", "success", "completed"}:
            status = STATUS_PAID
        now_text = _now_text()
        with Session(engine) as session:
            model = session.get(BaxiGptCdkPoolModel, int(record_id or 0))
            if model is None:
                return None
            model.status = status
            model.order_id = str(response.get("order_id") or model.order_id or "").strip()
            model.display_id = str(response.get("display_id") or model.display_id or "").strip()
            model.remote_email = str(response.get("email") or model.remote_email or "").strip()
            model.upstream_status = raw_status
            model.submit_response_json = safe_json(response)
            model.submitted_at = now_text
            model.last_checked_at = now_text
            if status == STATUS_PAID:
                model.paid_at = now_text
            model.last_error_code = ""
            model.last_error_message = ""
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def mark_failure(self, record_id: int, *, error_code: str = "", error_message: str = "", response: dict[str, Any] | None = None) -> BaxiGptCdkRecord | None:
        with Session(engine) as session:
            model = session.get(BaxiGptCdkPoolModel, int(record_id or 0))
            if model is None:
                return None
            model.status = STATUS_FAILED
            model.upstream_status = str((response or {}).get("status") or model.upstream_status or "failed")
            if response:
                model.submit_response_json = safe_json(response)
            model.last_error_code = str(error_code or "")[:80]
            model.last_error_message = str(error_message or "")[:1000]
            model.last_checked_at = _now_text()
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def mark_status_response(self, record_id: int, response: dict[str, Any]) -> BaxiGptCdkRecord | None:
        raw_status = str(response.get("status") or "").strip().lower()
        if raw_status in {"paid", "success", "completed"}:
            status = STATUS_PAID
        elif raw_status in {"processing", "pending", "submitted"}:
            status = STATUS_PROCESSING
        elif raw_status in {"failed", "expired", "cancelled", "canceled", "invalid", "used"}:
            status = STATUS_FAILED
        else:
            status = ""
        now_text = _now_text()
        with Session(engine) as session:
            model = session.get(BaxiGptCdkPoolModel, int(record_id or 0))
            if model is None:
                return None
            if status:
                model.status = status
            model.upstream_status = raw_status or model.upstream_status
            model.last_status_response_json = safe_json(response)
            model.remote_email = str(response.get("email") or model.remote_email or "").strip()
            model.display_id = str(response.get("display_id") or model.display_id or "").strip()
            model.order_id = str(response.get("order_id") or model.order_id or "").strip()
            model.last_checked_at = now_text
            if status == STATUS_PAID and not model.paid_at:
                model.paid_at = now_text
            if status == STATUS_FAILED:
                model.last_error_code = raw_status or "upstream_failed"
                model.last_error_message = str(response.get("message") or response.get("msg") or response.get("error") or "上游状态失败")[:1000]
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def persist_bound_account_extra(self, record: BaxiGptCdkRecord | None) -> bool:
        """把卡密订单状态同步到绑定账号的 extra.baxigpt_cdk。"""
        if record is None:
            return False
        account_id = int(record.bound_account_id or 0)
        email = str(record.bound_account_email or "").strip()
        with Session(engine) as session:
            account = session.get(AccountModel, account_id) if account_id > 0 else None
            if account is None and email:
                account = session.exec(
                    select(AccountModel)
                    .where(AccountModel.platform == "chatgpt")
                    .where(AccountModel.email == email)
                ).first()
            if account is None:
                return False
            self.persist_account_binding_extra(account, record)
            session.add(account)
            session.commit()
            return True

    def mark_query_response(self, code: str, response: dict[str, Any]) -> BaxiGptCdkRecord | None:
        code_hash = hash_code(code)
        if not code_hash:
            return None
        paid_statuses = {"paid", "success", "completed"}
        processing_statuses = {"processing", "pending", "submitted"}
        failed_statuses = {"failed", "expired", "cancelled", "canceled", "invalid", "used"}
        raw_status = str(response.get("status") or "").strip().lower()
        raw_status_code = str(response.get("status_code") or "").strip().lower()
        remaining_present = "remaining" in response
        total_present = "total" in response
        used_present = "used" in response
        remaining = _response_int(response, "remaining", 0)
        total = _response_int(response, "total", 0)
        used = _response_int(response, "used", 0)
        orders = [
            order for order in (response.get("orders") if isinstance(response.get("orders"), list) else [])
            if isinstance(order, dict)
        ]
        paid_orders = [order for order in orders if str(order.get("status") or "").strip().lower() in paid_statuses]
        processing_orders = [order for order in orders if str(order.get("status") or "").strip().lower() in processing_statuses]
        failed_orders = [order for order in orders if str(order.get("status") or "").strip().lower() in failed_statuses]
        chosen_order = (processing_orders or paid_orders or failed_orders or [{}])[0]
        chosen_status = str(chosen_order.get("status") or "").strip().lower() if isinstance(chosen_order, dict) else ""
        chosen_email = str(chosen_order.get("email") or "").strip() if isinstance(chosen_order, dict) else ""
        chosen_display_id = str(chosen_order.get("display_id") or "").strip() if isinstance(chosen_order, dict) else ""
        chosen_created_at = str(chosen_order.get("created_at") or "").strip() if isinstance(chosen_order, dict) else ""
        chosen_paid_at = str(chosen_order.get("paid_at") or "").strip() if isinstance(chosen_order, dict) else ""
        with Session(engine) as session:
            model = session.exec(
                select(BaxiGptCdkPoolModel).where(BaxiGptCdkPoolModel.code_hash == code_hash)
            ).first()
            if model is None:
                return None
            model.last_query_response_json = safe_json(response)
            model.last_checked_at = _now_text()
            if remaining_present:
                model.code_info_remaining = remaining
            if total_present:
                model.code_info_total = total

            current_status = str(model.status or STATUS_AVAILABLE)
            manual_disabled = current_status == STATUS_DISABLED
            if chosen_display_id:
                model.display_id = chosen_display_id
            if chosen_email:
                model.remote_email = chosen_email
            if chosen_created_at and not model.submitted_at:
                model.submitted_at = chosen_created_at

            if not bool(response.get("ok")):
                if not manual_disabled:
                    model.status = STATUS_FAILED
                model.last_error_code = raw_status or raw_status_code or "query_failed"
                model.last_error_message = response_message(response, "卡密查询失败")[:1000]
            elif processing_orders:
                if not manual_disabled:
                    model.status = STATUS_PROCESSING
                if chosen_email:
                    model.bound_account_email = chosen_email
                    account = session.exec(
                        select(AccountModel)
                        .where(AccountModel.platform == "chatgpt")
                        .where(AccountModel.email == chosen_email)
                    ).first()
                    if account is not None:
                        model.bound_account_id = int(account.id or 0)
                model.last_error_code = ""
                model.last_error_message = ""
            elif remaining_present and remaining > 0:
                if not manual_disabled:
                    model.status = STATUS_AVAILABLE
                    _clear_available_binding(model)
                model.last_error_code = ""
                model.last_error_message = ""
            elif raw_status in paid_statuses:
                if not manual_disabled:
                    model.status = STATUS_PAID
                if not model.paid_at:
                    model.paid_at = chosen_paid_at or _now_text()
                model.last_error_code = ""
                model.last_error_message = ""
            elif paid_orders:
                paid_order = paid_orders[0]
                paid_email = str(paid_order.get("email") or "").strip()
                paid_display_id = str(paid_order.get("display_id") or "").strip()
                paid_created_at = str(paid_order.get("created_at") or "").strip()
                paid_paid_at = str(paid_order.get("paid_at") or "").strip()
                if not manual_disabled:
                    model.status = STATUS_PAID
                if paid_display_id:
                    model.display_id = paid_display_id
                if paid_email:
                    model.remote_email = paid_email
                    model.bound_account_email = paid_email
                    account = session.exec(
                        select(AccountModel)
                        .where(AccountModel.platform == "chatgpt")
                        .where(AccountModel.email == paid_email)
                    ).first()
                    if account is not None:
                        model.bound_account_id = int(account.id or 0)
                if paid_created_at and not model.submitted_at:
                    model.submitted_at = paid_created_at
                if not model.paid_at:
                    model.paid_at = paid_paid_at or _now_text()
                model.last_error_code = ""
                model.last_error_message = ""
            elif (remaining_present and remaining <= 0) or (used_present and used > 0 and not orders and not str(model.order_id or "").strip()):
                if not manual_disabled:
                    model.status = STATUS_FAILED
                model.last_error_code = "quota_exhausted"
                model.last_error_message = "卡密配额已用完，但未查询到成功订单" if not paid_orders else "卡密配额已用完"
            elif raw_status in failed_statuses or failed_orders:
                if not manual_disabled:
                    model.status = STATUS_FAILED
                model.last_error_code = raw_status or chosen_status or "query_order_failed"
                model.last_error_message = response_message(response, "卡密查询返回订单失败")[:1000]
            if str(model.status or "") == STATUS_AVAILABLE:
                model.upstream_status = raw_status or raw_status_code or chosen_status or model.upstream_status
            else:
                model.upstream_status = raw_status or chosen_status or raw_status_code or model.upstream_status
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
            session.refresh(model)
            return _to_record(model)

    def persist_account_binding_extra(
        self,
        account: AccountModel,
        record: BaxiGptCdkRecord,
        *,
        status: str | None = None,
        upstream_status: str | None = None,
        order_id: str | None = None,
        display_id: str | None = None,
        last_error_message: str | None = None,
    ) -> None:
        try:
            extra = account.get_extra()
        except Exception:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        target_status = status if status is not None else record.status
        payload = {
            "status": target_status,
            "upstream_status": upstream_status if upstream_status is not None else record.upstream_status,
            "code_masked": record.code_masked,
            "cdk_id": record.id,
            "order_id": order_id if order_id is not None else record.order_id,
            "display_id": display_id if display_id is not None else record.display_id,
            "remote_email": account.email or record.remote_email,
            "task_id": record.task_id,
            "bound_at": record.bound_at,
            "submitted_at": record.submitted_at,
            "paid_at": record.paid_at,
            "last_checked_at": record.last_checked_at,
            "last_error_message": last_error_message if last_error_message is not None else record.last_error_message,
        }
        extra["baxigpt_cdk"] = payload
        history = extra.get("baxigpt_cdk_history")
        if not isinstance(history, list):
            history = []
        last_history = history[-1] if history and isinstance(history[-1], dict) else {}
        history_changed = any(
            str(last_history.get(key) or "") != str(payload.get(key) or "")
            for key in ("status", "upstream_status", "order_id", "display_id", "last_error_message")
        )
        if not history or history_changed:
            history.append(payload)
        extra["baxigpt_cdk_history"] = history[-20:]
        account.set_extra(extra)
        status_text = str(target_status or "").strip().lower()
        if status_text == STATUS_PAID:
            mark_payment_succeeded(account, reason="baxigpt_cdk_paid")
        elif status_text == STATUS_FAILED:
            mark_payment_failed(account, reason="baxigpt_cdk_failed")
        account.updated_at = _utcnow()

    def mark_account_ineligible(self, account: AccountModel, record: BaxiGptCdkRecord, reason: str) -> None:
        try:
            extra = account.get_extra()
        except Exception:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        extra["chatgpt_account_unavailable"] = True
        extra["chatgpt_unavailable_reason"] = reason
        extra["chatgpt_skip_save_account"] = True
        extra["chatgpt_skip_save_reason"] = reason
        extra["chatgpt_invalid_registration_failure"] = True
        extra["chatgpt_invalid_registration_reason"] = reason
        account.set_extra(extra)
        self.persist_account_binding_extra(
            account,
            record,
            status=STATUS_FAILED,
            upstream_status="failed",
            last_error_message=reason,
        )
