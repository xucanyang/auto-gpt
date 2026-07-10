from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from curl_cffi import requests as cffi_requests
from sqlmodel import Session

from core.config_store import config_store
from core.db import AccountModel
from services.chatgpt_core.sub2api_upload import build_sub2api_lookup_payload, upload_to_sub2api_detailed
from services.chatgpt_account_state import classify_chatgpt_capabilities
from services.chatgpt_sync import build_chatgpt_sync_account

SUB2API_SYNC_NAME = "sub2api"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _get_account_extra(account: Any) -> dict[str, Any]:
    if hasattr(account, "get_extra"):
        try:
            extra = account.get_extra()
            if isinstance(extra, dict):
                return extra
        except Exception:
            pass
    extra = getattr(account, "extra", {})
    return extra if isinstance(extra, dict) else {}


def get_sub2api_sync_state(extra_or_account: Any) -> dict[str, Any]:
    extra = extra_or_account if isinstance(extra_or_account, dict) else _get_account_extra(extra_or_account)
    sync_statuses = extra.get("sync_statuses", {})
    if not isinstance(sync_statuses, dict):
        return {}
    state = sync_statuses.get(SUB2API_SYNC_NAME, {})
    return state if isinstance(state, dict) else {}


def record_sub2api_sync_result(extra: dict[str, Any], sync_result: dict[str, Any]) -> dict[str, Any]:
    sync_statuses = extra.get("sync_statuses")
    if not isinstance(sync_statuses, dict):
        sync_statuses = {}

    current = sync_statuses.get(SUB2API_SYNC_NAME)
    if not isinstance(current, dict):
        current = {}

    merged = dict(current)
    sync_result = dict(sync_result or {})
    merged.update(sync_result)
    merged["last_attempt_at"] = _utcnow_iso()

    for transient_key in ("candidate_count", "candidates", "remote_account_id", "status"):
        if transient_key not in sync_result:
            merged.pop(transient_key, None)

    remote_state = _safe_str(merged.get("remote_state")).lower()
    uploaded = bool(merged.get("uploaded")) or remote_state in {"exists", "uploaded"}
    merged["uploaded"] = uploaded
    if uploaded:
        merged["uploaded_at"] = _safe_str(merged.get("uploaded_at")) or _utcnow_iso()
    else:
        merged.pop("uploaded_at", None)
    if "message" in merged:
        merged["last_message"] = _safe_str(merged.get("message"))

    sync_statuses[SUB2API_SYNC_NAME] = merged
    extra["sync_statuses"] = sync_statuses
    return merged


def update_account_model_sub2api_sync(
    account: AccountModel,
    sync_result: dict[str, Any],
    session: Session | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    extra = account.get_extra()
    state = record_sub2api_sync_result(extra, sync_result)
    extra["chatgpt_capabilities"] = classify_chatgpt_capabilities(account)
    account.set_extra(extra)
    account.updated_at = _utcnow()
    if session is not None:
        session.add(account)
        from services.account_filters import upsert_account_list_state_for_account_ids

        upsert_account_list_state_for_account_ids(session, [account.id], commit=False)
        if commit:
            session.commit()
            session.refresh(account)
    return state


def _get_config_value(key: str, default: str = "") -> str:
    try:
        return _safe_str(config_store.get(key, default) or default)
    except Exception:
        return default


def _build_probe_identity(account: Any) -> dict[str, str]:
    sync_account = account if getattr(account, "access_token", None) else build_chatgpt_sync_account(account)
    payload = build_sub2api_lookup_payload(sync_account)
    credentials = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else {}
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    email = _safe_str(extra.get("email") or payload.get("name") or getattr(account, "email", ""))
    return {
        "email": email,
        "organization_id": _safe_str(credentials.get("organization_id")),
        "chatgpt_account_id": _safe_str(credentials.get("chatgpt_account_id")),
        "chatgpt_user_id": _safe_str(credentials.get("chatgpt_user_id")),
    }


def _candidate_matches(row: dict[str, Any], identity: dict[str, str]) -> list[str]:
    matches: list[str] = []
    email = identity.get("email", "")
    credentials = row.get("credentials") if isinstance(row.get("credentials"), dict) else {}
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}

    if email and (_safe_str(row.get("name")) == email or _safe_str(extra.get("email")) == email):
        matches.append("email")

    organization_id = identity.get("organization_id", "")
    chatgpt_account_id = identity.get("chatgpt_account_id", "")
    if (
        organization_id
        and chatgpt_account_id
        and _safe_str(credentials.get("organization_id")) == organization_id
        and _safe_str(credentials.get("chatgpt_account_id")) == chatgpt_account_id
    ):
        matches.append("organization_account")

    chatgpt_user_id = identity.get("chatgpt_user_id", "")
    if chatgpt_user_id and _safe_str(credentials.get("chatgpt_user_id")) == chatgpt_user_id:
        matches.append("chatgpt_user_id")

    return matches


def _identity_prefers_exact_workspace(identity: dict[str, str]) -> bool:
    return bool(identity.get("organization_id") and identity.get("chatgpt_account_id"))


def _collect_probe_candidates(rows: list[dict[str, Any]], identity: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    exact_candidates: list[dict[str, Any]] = []
    weak_candidates: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        matched_by = _candidate_matches(row, identity)
        if not matched_by:
            continue
        candidate = {
            "id": row.get("id"),
            "name": row.get("name") or "",
            "status": row.get("status") or "",
            "matched_by": matched_by,
            "updated_at": row.get("updated_at") or "",
        }
        candidates.append(candidate)
        if "organization_account" in matched_by:
            exact_candidates.append(candidate)
        else:
            weak_candidates.append(candidate)

    return candidates, exact_candidates, weak_candidates


def _extract_api_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        items = data.get("items")
    else:
        items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _fetch_sub2api_account_items(identity: dict[str, str]) -> list[dict[str, Any]]:
    api_url = _get_config_value("sub2api_api_url")
    api_key = _get_config_value("sub2api_api_key")
    if not api_url:
        raise RuntimeError("Sub2API API URL 未配置")
    if not api_key:
        raise RuntimeError("Sub2API API Key 未配置")

    # 官方 accounts 列表 API 目前按 name search；纯 API 模式不再回退 DB。
    search = identity.get("email") or identity.get("chatgpt_user_id") or identity.get("chatgpt_account_id") or ""
    if not search:
        return []

    response = cffi_requests.get(
        f"{api_url.rstrip('/')}/api/v1/admin/accounts",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{api_url.rstrip('/')}/admin/accounts",
            "x-api-key": api_key,
        },
        params={
            "platform": "openai",
            "type": "oauth",
            "search": search,
            "page": 1,
            "page_size": int(_get_config_value("sub2api_probe_api_page_size", "50") or 50),
        },
        proxies=None,
        verify=False,
        timeout=float(_get_config_value("sub2api_probe_timeout_seconds", "15") or 15),
        impersonate="chrome110",
    )
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = _safe_str(body.get("message") or body.get("error") or body.get("msg"))
        except Exception:
            detail = (response.text or "")[:200]
        raise RuntimeError(f"Sub2API API 返回 HTTP {response.status_code}{(': ' + detail) if detail else ''}")
    return _extract_api_items(response.json())


def _build_exists_state(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "remote_state": "exists",
        "uploaded": True,
        "remote_account_id": candidate.get("id"),
        "status": candidate.get("status") or "",
        "matched_by": ", ".join(candidate.get("matched_by") or []),
        "message": f"远端已存在 Sub2API 账号 (#{candidate.get('id')})",
        "checked_at": _utcnow_iso(),
        "probe_source": "api",
    }


def probe_chatgpt_sub2api_status(account: Any) -> dict[str, Any]:
    """纯 API 探测 Sub2API 远端账号状态，不再使用数据库直连。"""
    identity = _build_probe_identity(account)
    if not any(identity.values()):
        return {
            "remote_state": "not_found",
            "uploaded": False,
            "matched_by": "",
            "message": "缺少可用于 Sub2API API 探测的标识",
            "checked_at": _utcnow_iso(),
            "probe_source": "api",
        }

    try:
        rows = _fetch_sub2api_account_items(identity)
    except Exception as exc:
        return {
            "remote_state": "unreachable",
            "uploaded": False,
            "matched_by": "",
            "message": f"Sub2API API 不可用: {exc}",
            "checked_at": _utcnow_iso(),
            "probe_source": "api",
        }

    candidates, exact_candidates, weak_candidates = _collect_probe_candidates(rows, identity)
    prefers_exact_workspace = _identity_prefers_exact_workspace(identity)

    if prefers_exact_workspace:
        if len(exact_candidates) == 1:
            return _build_exists_state(exact_candidates[0])
        if len(exact_candidates) > 1:
            return {
                "remote_state": "ambiguous",
                "uploaded": False,
                "matched_by": ", ".join(sorted({item for candidate in exact_candidates for item in candidate["matched_by"]})),
                "message": f"远端 API 匹配到 {len(exact_candidates)} 条精确 Sub2API 记录，已跳过上传",
                "candidate_count": len(exact_candidates),
                "candidates": exact_candidates,
                "checked_at": _utcnow_iso(),
                "probe_source": "api",
            }
        if weak_candidates:
            return {
                "remote_state": "cross_workspace_only",
                "uploaded": False,
                "matched_by": ", ".join(sorted({item for candidate in weak_candidates for item in candidate["matched_by"]})),
                "message": "仅通过 Sub2API API 命中同邮箱/同用户的其他 workspace，可为当前 workspace 补传",
                "candidate_count": len(weak_candidates),
                "candidates": weak_candidates,
                "checked_at": _utcnow_iso(),
                "probe_source": "api",
            }
        return {
            "remote_state": "not_found",
            "uploaded": False,
            "matched_by": "",
            "message": "远端 API 未发现 Sub2API 账号",
            "checked_at": _utcnow_iso(),
            "probe_source": "api",
        }

    if not candidates:
        return {
            "remote_state": "not_found",
            "uploaded": False,
            "matched_by": "",
            "message": "远端 API 未发现 Sub2API 账号",
            "checked_at": _utcnow_iso(),
            "probe_source": "api",
        }

    if len(candidates) > 1:
        return {
            "remote_state": "ambiguous",
            "uploaded": False,
            "matched_by": ", ".join(sorted({item for candidate in candidates for item in candidate["matched_by"]})),
            "message": f"远端 API 匹配到 {len(candidates)} 条 Sub2API 记录，已跳过上传",
            "candidate_count": len(candidates),
            "candidates": candidates,
            "checked_at": _utcnow_iso(),
            "probe_source": "api",
        }

    return _build_exists_state(candidates[0])


def _last_upload(status: str, action: str, message: str, *, started_at: str, finished_at: str | None = None, **extra: Any) -> dict[str, Any]:
    payload = {
        "status": status,
        "action": action,
        "message": message,
        "attempted_at": started_at,
        "finished_at": finished_at or _utcnow_iso(),
    }
    payload.update({key: value for key, value in extra.items() if value not in (None, "")})
    return payload


def _build_upload_failure_state(message: str, *, started_at: str, initial_sync: dict[str, Any] | None = None) -> dict[str, Any]:
    initial_sync = dict(initial_sync or {})
    remote_state = _safe_str(initial_sync.get("remote_state")).lower() or "unknown"
    return {
        **initial_sync,
        "remote_state": remote_state,
        "uploaded": bool(initial_sync.get("uploaded")) or remote_state in {"exists", "uploaded"},
        "message": message,
        "checked_at": _safe_str(initial_sync.get("checked_at")) or _utcnow_iso(),
        "probe_source": _safe_str(initial_sync.get("probe_source")) or "upload",
        "last_upload": _last_upload(
            "failed",
            "upload",
            message,
            started_at=started_at,
            probe_before=initial_sync.get("remote_state") or "",
        ),
    }


def _build_upload_success_state(result: dict[str, Any], *, started_at: str, initial_sync: dict[str, Any] | None = None) -> dict[str, Any]:
    initial_sync = dict(initial_sync or {})
    message = _safe_str(result.get("message")) or "上传成功"
    remote_account_id = result.get("remote_account_id") or initial_sync.get("remote_account_id")
    remote_status = _safe_str(result.get("remote_status")) or _safe_str(initial_sync.get("status"))
    return {
        "remote_state": "uploaded",
        "uploaded": True,
        "remote_account_id": remote_account_id,
        "status": remote_status,
        "message": message,
        "checked_at": _utcnow_iso(),
        "uploaded_at": _utcnow_iso(),
        "probe_source": "upload",
        "last_upload": _last_upload(
            "success",
            "upload",
            message,
            started_at=started_at,
            remote_account_id=remote_account_id,
            remote_status=remote_status,
            probe_before=initial_sync.get("remote_state") or "",
            probe_after="uploaded",
        ),
    }


def backfill_chatgpt_account_to_sub2api(
    account: AccountModel,
    *,
    session: Session | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    started_at = _utcnow_iso()
    cached_sync = get_sub2api_sync_state(account)
    sync_account = build_chatgpt_sync_account(account)
    try:
        upload_result = upload_to_sub2api_detailed(sync_account)
    except Exception as exc:
        upload_result = {"ok": False, "message": f"上传异常: {exc}"}
    ok = bool(upload_result.get("ok"))
    msg = _safe_str(upload_result.get("message")) or ("上传成功" if ok else "上传失败")
    upload_state = (
        _build_upload_success_state(upload_result, started_at=started_at, initial_sync=cached_sync)
        if ok
        else _build_upload_failure_state(msg, started_at=started_at, initial_sync=cached_sync)
    )
    update_account_model_sub2api_sync(account, upload_state, session=session, commit=False)
    results.append({"name": "Sub2API 上传", "ok": ok, "msg": msg})

    if not ok:
        if session is not None and commit:
            session.commit()
            session.refresh(account)
        return {"ok": False, "uploaded": False, "skipped": False, "message": msg, "results": results}

    verify_msg = upload_state.get("message") or "上传成功"
    results.append({"name": "Sub2API 确认", "ok": True, "msg": f"以上传接口返回为准：{verify_msg}"})
    if session is not None and commit:
        session.commit()
        session.refresh(account)
    return {"ok": True, "uploaded": True, "skipped": False, "message": verify_msg, "results": results}
