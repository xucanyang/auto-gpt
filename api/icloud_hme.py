from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import csv
import io

from core.base_mailbox import ICloudHmeClient
from core.db import (
    bulk_enable_icloud_hme_aliases,
    bulk_disable_used_icloud_hme_aliases,
    claim_icloud_hme_alias,
    get_icloud_hme_alias_by_anonymous_id,
    import_icloud_hme_alias_rows,
    list_icloud_hme_aliases,
    mark_icloud_hme_alias_used,
    patch_icloud_hme_alias,
    set_icloud_hme_alias_enabled,
)

router = APIRouter(prefix="/icloud-hme", tags=["icloud-hme"])

ALLOWED_STATUSES = {
    "reserved",
    "registered",
    "register_failed",
    "in_use",
    "retired",
}


class IcloudHmeAliasPatchRequest(BaseModel):
    status: str | None = None
    note: str | None = None
    purpose: str | None = None
    bound_service: str | None = None
    bound_account_email: str | None = None
    bound_account_ref: str | None = None


class IcloudHmeImportCsvRequest(BaseModel):
    content: str
    purpose: str = "chatgpt_register"
    bound_service: str = "chatgpt"
    forward_to: str = "b@cccy.me"


class IcloudHmeSyncRequest(BaseModel):
    icloud_cookie: str
    icloud_domain_base: str = "icloud.com"
    forward_to: str = "b@cccy.me"
    purpose: str = "chatgpt_register"
    bound_service: str = "chatgpt"


class IcloudHmeToggleEnabledRequest(BaseModel):
    enabled: bool


class IcloudHmeBulkEnableRequest(BaseModel):
    forward_to: str = "b@cccy.me"
    only_manual_created: bool = False
    only_unused: bool = True


class IcloudHmeMarkUsedRequest(BaseModel):
    note: str = "manually_copied"


def _pick_first_non_empty(row: dict, keys: list[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _normalize_import_status(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"registered", "register_failed", "in_use", "retired"}:
        return text
    return "reserved"


def _normalize_sync_status(item: dict) -> str:
    if not isinstance(item, dict):
        return "reserved"
    is_active = item.get("isActive")
    if is_active is False:
        return "retired"
    return "reserved"


def _parse_csv_rows(content: str) -> list[dict]:
    text = str(content or "").replace("\ufeff", "").strip()
    if not text:
        return []
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    for raw in reader:
        row = {str(k or "").strip(): v for k, v in (raw or {}).items()}
        anonymous_id = _pick_first_non_empty(
            row,
            ["anonymous_id", "anonymousId", "id", "mailId", "mail_id"],
        )
        hme = _pick_first_non_empty(
            row,
            ["hme", "email", "address", "alias", "full_address"],
        )
        if not anonymous_id or not hme:
            continue
        rows.append(
            {
                "anonymous_id": anonymous_id,
                "hme": hme,
                "label": _pick_first_non_empty(row, ["label", "name"]),
                "note": _pick_first_non_empty(row, ["note", "comment", "memo"]),
                "forward_to": _pick_first_non_empty(row, ["forward_to", "forwardTo", "recipient", "target"]),
                "status": _normalize_import_status(_pick_first_non_empty(row, ["status", "state"])),
                "bound_account_email": _pick_first_non_empty(row, ["bound_account_email", "boundEmail"]),
                "bound_account_ref": _pick_first_non_empty(row, ["bound_account_ref", "boundRef"]),
                "task_id": _pick_first_non_empty(row, ["task_id", "taskId"]),
                "last_otp_at": _pick_first_non_empty(row, ["last_otp_at", "lastOtpAt"]),
                "last_error": _pick_first_non_empty(row, ["last_error", "lastError"]),
            }
        )
    return rows


def _extract_hme_list_rows(payload: object, *, forward_to: str) -> list[dict]:
    def _find_alias_list(value: object) -> list[dict] | None:
        if isinstance(value, list):
            if all(isinstance(item, dict) for item in value):
                first = value[0] if value else {}
                if not value or (
                    isinstance(first, dict)
                    and (
                        _pick_first_non_empty(first, ["anonymousId", "anonymous_id", "id"])
                        or _pick_first_non_empty(first, ["hme", "email", "address"])
                    )
                ):
                    return value
            for item in value:
                nested = _find_alias_list(item)
                if nested is not None:
                    return nested
            return None
        if isinstance(value, dict):
            for key in ("hmeEmails", "aliases", "items", "data", "result", "hme"):
                nested = value.get(key)
                found = _find_alias_list(nested)
                if found is not None:
                    return found
            for nested in value.values():
                found = _find_alias_list(nested)
                if found is not None:
                    return found
        return None

    payload = _find_alias_list(payload) or []
    rows: list[dict] = []
    if not isinstance(payload, list):
        return rows
    for item in payload:
        if not isinstance(item, dict):
            continue
        anonymous_id = _pick_first_non_empty(item, ["anonymousId", "anonymous_id", "id"])
        hme = _pick_first_non_empty(item, ["hme", "email", "address"])
        resolved_forward_to = _pick_first_non_empty(
            item,
            ["forwardToEmail", "forward_to", "forwardTo", "recipient", "target"],
        ) or str(forward_to or "").strip()
        if not anonymous_id or not hme:
            continue
        rows.append(
            {
                "anonymous_id": anonymous_id,
                "hme": hme,
                "label": _pick_first_non_empty(item, ["label", "name"]),
                "note": _pick_first_non_empty(item, ["note", "comment"]),
                "forward_to": resolved_forward_to,
                "status": _normalize_sync_status(item),
                "created_source": "manual_created",
                "record_source": "icloud_sync",
                "last_synced_at": "",
            }
        )
    return rows


@router.get("/aliases")
def get_aliases(
    page: int = 1,
    size: int = 20,
    status: str = "",
    purpose: str = "",
    bound_service: str = "",
    hme: str = "",
    bound_account_email: str = "",
    enabled: str = "",
    created_source: str = "",
    ready_only: bool = False,
    forward_to: str = "",
):
    return list_icloud_hme_aliases(
        page=page,
        size=size,
        status=status,
        purpose=purpose,
        bound_service=bound_service,
        hme=hme,
        bound_account_email=bound_account_email,
        enabled=enabled,
        created_source=created_source,
        ready_only=ready_only,
        forward_to=forward_to,
    )


@router.get("/aliases/{anonymous_id}")
def get_alias(anonymous_id: str):
    row = get_icloud_hme_alias_by_anonymous_id(anonymous_id)
    if row is None:
        raise HTTPException(status_code=404, detail="alias not found")
    return row


@router.patch("/aliases/{anonymous_id}")
def patch_alias(anonymous_id: str, body: IcloudHmeAliasPatchRequest):
    try:
        payload = body.model_dump(exclude_none=True)
    except AttributeError:
        payload = body.dict(exclude_none=True)
    updates = {k: v for k, v in payload.items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="no fields to update")

    next_status = str(updates.get("status") or "").strip()
    if next_status and next_status not in ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid status: {next_status}",
        )

    try:
        return patch_icloud_hme_alias(anonymous_id, updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="alias not found") from exc


@router.post("/import-csv")
def import_aliases_csv(body: IcloudHmeImportCsvRequest):
    rows = _parse_csv_rows(body.content)
    if not rows:
        raise HTTPException(status_code=400, detail="no valid rows found in csv")
    return import_icloud_hme_alias_rows(
        rows,
        purpose=str(body.purpose or "chatgpt_register").strip() or "chatgpt_register",
        bound_service=str(body.bound_service or "chatgpt").strip() or "chatgpt",
        default_forward_to=str(body.forward_to or "b@cccy.me").strip() or "b@cccy.me",
    )


@router.post("/claim")
def claim_alias(
    task_id: str = "",
    purpose: str = "chatgpt_register",
    bound_service: str = "chatgpt",
    forward_to: str = "",
):
    row = claim_icloud_hme_alias(
        task_id=task_id,
        purpose=purpose,
        bound_service=bound_service,
        forward_to=forward_to,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no available alias")
    return row


@router.post("/aliases/{anonymous_id}/enabled")
def toggle_alias_enabled(anonymous_id: str, body: IcloudHmeToggleEnabledRequest):
    try:
        return set_icloud_hme_alias_enabled(anonymous_id, bool(body.enabled))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="alias not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/aliases/bulk-enable")
def bulk_enable_aliases(body: IcloudHmeBulkEnableRequest):
    try:
        return bulk_enable_icloud_hme_aliases(
            forward_to=str(body.forward_to or "b@cccy.me").strip() or "b@cccy.me",
            only_manual_created=bool(body.only_manual_created),
            only_unused=bool(body.only_unused),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/aliases/bulk-disable-used")
def bulk_disable_used_aliases(body: IcloudHmeBulkEnableRequest):
    try:
        return bulk_disable_used_icloud_hme_aliases(
            forward_to=str(body.forward_to or "b@cccy.me").strip() or "b@cccy.me",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/aliases/{anonymous_id}/mark-used")
def mark_alias_used(anonymous_id: str, body: IcloudHmeMarkUsedRequest):
    try:
        return mark_icloud_hme_alias_used(
            anonymous_id,
            note=str(body.note or "manually_copied").strip() or "manually_copied",
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="alias not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sync-live")
def sync_live_aliases(body: IcloudHmeSyncRequest):
    cookie = str(body.icloud_cookie or "").strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="icloud_cookie is required")

    client = ICloudHmeClient(
        cookie=cookie,
        domain_base=str(body.icloud_domain_base or "icloud.com").strip() or "icloud.com",
        proxy=None,
    )
    try:
        payload = client._request_action("list", "GET", "/v2/hme/list", payload=None)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = _extract_hme_list_rows(
        payload,
        forward_to=str(body.forward_to or "b@cccy.me").strip() or "b@cccy.me",
    )
    for item in rows:
        item["purpose"] = str(body.purpose or "chatgpt_register").strip() or "chatgpt_register"
        item["bound_service"] = str(body.bound_service or "chatgpt").strip() or "chatgpt"
        item["last_synced_at"] = item.get("last_synced_at") or ""

        existing = get_icloud_hme_alias_by_anonymous_id(item["anonymous_id"])
        if existing and bool(existing.get("used_by_system")):
            item["created_source"] = str(existing.get("created_source") or "manual_created")
            item["status"] = str(existing.get("status") or item.get("status") or "reserved").strip() or "reserved"
            item["use_count"] = int(existing.get("use_count") or 0)
            item["first_claimed_at"] = str(existing.get("first_claimed_at") or "")
            item["last_claimed_at"] = str(existing.get("last_claimed_at") or "")
            item["last_otp_at"] = str(existing.get("last_otp_at") or "")
            item["bound_account_email"] = str(existing.get("bound_account_email") or "")
            item["bound_account_ref"] = str(existing.get("bound_account_ref") or "")
            item["task_id"] = str(existing.get("task_id") or "")
            item["last_error"] = str(existing.get("last_error") or "")
        elif existing and str(existing.get("created_source") or "").strip():
            item["created_source"] = str(existing.get("created_source") or "").strip()

    return {
        "synced_count": len(rows),
        "result": import_icloud_hme_alias_rows(
            rows,
            purpose=str(body.purpose or "chatgpt_register").strip() or "chatgpt_register",
            bound_service=str(body.bound_service or "chatgpt").strip() or "chatgpt",
            default_forward_to=str(body.forward_to or "b@cccy.me").strip() or "b@cccy.me",
        ),
    }


@router.get("/auto-pool/status")
def get_auto_pool_status():
    from services.icloud_hme_auto_pool import get_status

    return get_status()
