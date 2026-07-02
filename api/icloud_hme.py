from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import csv
import io

from core.base_mailbox import ICloudHmeClient
from core.db import (
    bulk_enable_icloud_hme_aliases,
    bulk_disable_used_icloud_hme_aliases,
    claim_icloud_hme_alias,
    create_icloud_hme_recheck_campaign,
    get_icloud_hme_alias_by_anonymous_id,
    get_icloud_hme_recheck_campaign,
    import_icloud_hme_alias_rows,
    list_icloud_hme_aliases,
    list_icloud_hme_deletion_candidates,
    list_icloud_hme_recheck_campaigns,
    mark_icloud_hme_alias_used,
    patch_icloud_hme_alias,
    prune_icloud_hme_aliases_not_in_remote,
    release_stale_icloud_hme_recheck_running,
    reset_icloud_hme_aliases_for_rerun,
    set_icloud_hme_alias_enabled,
)

router = APIRouter(prefix="/icloud-hme", tags=["icloud-hme"])

ALLOWED_STATUSES = {
    "reserved",
    "registered",
    "register_failed",
    "account_deactivated",
    "account_disabled",
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
    # True 时，以 iCloud 官网 list 结果为准，删除本地别名池中已不存在于官网的行。
    # 这只清理本地 icloud_hme_alias 记录，不删除 ChatGPT 账号，也不触发 Apple 端 delete。
    prune_missing: bool = False
    dry_run: bool = False


class IcloudHmeToggleEnabledRequest(BaseModel):
    enabled: bool


class IcloudHmeBulkEnableRequest(BaseModel):
    forward_to: str = "b@cccy.me"
    only_manual_created: bool = False
    only_unused: bool = True


class IcloudHmeMarkUsedRequest(BaseModel):
    note: str = "manually_copied"


class IcloudHmeAutoDeleteRunRequest(BaseModel):
    force: bool = True
    ignore_active_tasks: bool = False
    delete: bool = True


class IcloudHmeRecheckCampaignCreateRequest(BaseModel):
    campaign_id: str = ""
    purpose: str = "chatgpt_register"
    bound_service: str = "chatgpt"
    forward_to: str = "b@cccy.me"
    include_ready_stock: bool = False
    include_in_flight: bool = False
    reset_existing: bool = False


class IcloudHmeResetRerunRequest(BaseModel):
    campaign_id: str = ""
    purpose: str = "chatgpt_register"
    bound_service: str = "chatgpt"
    forward_to: str = "b@cccy.me"
    include_in_flight: bool = False
    include_ready_stock: bool = False
    reset_existing_queue: bool = True
    dry_run: bool = False
    limit: int = 0


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
    if text in {"registered", "register_failed", "account_deactivated", "account_disabled", "in_use", "retired"}:
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


@router.post("/aliases/reset-rerun")
def reset_aliases_for_rerun(body: IcloudHmeResetRerunRequest):
    try:
        return reset_icloud_hme_aliases_for_rerun(
            campaign_id=str(body.campaign_id or "").strip(),
            purpose=str(body.purpose or "chatgpt_register").strip() or "chatgpt_register",
            bound_service=str(body.bound_service or "chatgpt").strip() or "chatgpt",
            forward_to=str(body.forward_to or "").strip(),
            include_in_flight=bool(body.include_in_flight),
            include_ready_stock=bool(body.include_ready_stock),
            reset_existing_queue=bool(body.reset_existing_queue),
            dry_run=bool(body.dry_run),
            limit=int(body.limit or 0),
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
    if bool(body.prune_missing) and not rows:
        raise HTTPException(
            status_code=400,
            detail="iCloud HME list returned no aliases; refusing to prune local aliases",
        )
    for item in rows:
        item["purpose"] = str(body.purpose or "chatgpt_register").strip() or "chatgpt_register"
        item["bound_service"] = str(body.bound_service or "chatgpt").strip() or "chatgpt"
        item["last_synced_at"] = item.get("last_synced_at") or ""

        existing = get_icloud_hme_alias_by_anonymous_id(item["anonymous_id"])
        if existing:
            # 同步官网列表不应该把已经启用的本地导入池全部打回停用；
            # 只有官网明确 inactive/retired 时才强制从池里移除。
            item["enabled"] = bool(existing.get("enabled")) and str(item.get("status") or "") != "retired"
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

    normalized_purpose = str(body.purpose or "chatgpt_register").strip() or "chatgpt_register"
    normalized_service = str(body.bound_service or "chatgpt").strip() or "chatgpt"
    normalized_forward_to = str(body.forward_to or "b@cccy.me").strip() or "b@cccy.me"
    import_result = import_icloud_hme_alias_rows(
        rows,
        purpose=normalized_purpose,
        bound_service=normalized_service,
        default_forward_to=normalized_forward_to,
    )
    prune_result = None
    if bool(body.prune_missing):
        prune_result = prune_icloud_hme_aliases_not_in_remote(
            {item["anonymous_id"] for item in rows},
            purpose=normalized_purpose,
            bound_service=normalized_service,
            forward_to=normalized_forward_to,
            dry_run=bool(body.dry_run),
        )

    return {
        "synced_count": len(rows),
        "result": import_result,
        "prune": prune_result,
    }


@router.get("/auto-pool/status")
def get_auto_pool_status():
    from services.icloud_hme_auto_pool import get_status

    return get_status()


@router.get("/auto-delete/status")
def get_auto_delete_status():
    from services.icloud_hme_auto_delete import get_status

    return get_status()


@router.post("/auto-delete/run")
def run_auto_delete(body: IcloudHmeAutoDeleteRunRequest | None = None):
    from services.icloud_hme_auto_delete import run_once

    payload = body or IcloudHmeAutoDeleteRunRequest()
    return run_once(
        force=payload.force,
        ignore_active_tasks=payload.ignore_active_tasks,
        delete=payload.delete,
    )


@router.get("/deletion-preview")
def get_deletion_preview():
    """只读预览：返回会被自动删除 worker 视为「可删」的别名清单（不测活、不触 Apple）。

    bound_invalid 表示「绑定账号当前为 invalid，删除前会先跑失效测活，能恢复则保留」。
    """
    analysis = list_icloud_hme_deletion_candidates()
    return {
        "summary": analysis.get("summary", {}),
        "total": analysis.get("total", 0),
        "orphan": analysis.get("orphan", []),
        "bound_invalid": analysis.get("bound_invalid", []),
        "protected_count": len(analysis.get("protected", [])),
    }


@router.post("/recheck/campaigns")
def create_recheck_campaign(body: IcloudHmeRecheckCampaignCreateRequest):
    """创建 iCloud HME 复测批次。

    这里只写本地复测队列，不调用 Apple deactivate/delete。
    """
    return create_icloud_hme_recheck_campaign(
        campaign_id=str(body.campaign_id or "").strip(),
        purpose=str(body.purpose or "chatgpt_register").strip() or "chatgpt_register",
        bound_service=str(body.bound_service or "chatgpt").strip() or "chatgpt",
        forward_to=str(body.forward_to or "").strip(),
        include_ready_stock=bool(body.include_ready_stock),
        include_in_flight=bool(body.include_in_flight),
        reset_existing=bool(body.reset_existing),
    )


@router.get("/recheck/campaigns")
def list_recheck_campaigns(limit: int = 20):
    return list_icloud_hme_recheck_campaigns(limit=limit)


@router.get("/recheck/campaigns/{campaign_id}")
def get_recheck_campaign(
    campaign_id: str,
    page: int = 1,
    size: int = 20,
    status: str = "",
    result_code: str = "",
    delete_candidate: str = "",
    hme: str = "",
):
    return get_icloud_hme_recheck_campaign(
        campaign_id,
        page=page,
        size=size,
        status=status,
        result_code=result_code,
        delete_candidate=delete_candidate,
        hme=hme,
    )


@router.get("/recheck/current")
def get_current_recheck_campaign(
    page: int = 1,
    size: int = 20,
    status: str = "",
    result_code: str = "",
    delete_candidate: str = "",
    hme: str = "",
):
    return get_icloud_hme_recheck_campaign(
        "",
        page=page,
        size=size,
        status=status,
        result_code=result_code,
        delete_candidate=delete_candidate,
        hme=hme,
    )


@router.post("/recheck/release-stale")
def release_stale_recheck_items(campaign_id: str = "", older_than_seconds: int = 7200):
    return release_stale_icloud_hme_recheck_running(
        campaign_id=str(campaign_id or "").strip(),
        older_than_seconds=int(older_than_seconds or 7200),
    )
