from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from sqlmodel import Session

from core.config_store import config_store
from core.db import AccountModel
from platforms.chatgpt.status_probe import probe_local_chatgpt_status
from platforms.chatgpt.sub2api_upload import build_sub2api_lookup_payload, upload_to_sub2api
from services.chatgpt_sync import build_chatgpt_sync_account, update_account_model_local_probe

SUB2API_SYNC_NAME = "sub2api"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


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

    remote_state = str(merged.get("remote_state") or "").strip().lower()
    uploaded = bool(merged.get("uploaded")) or remote_state == "exists"
    merged["uploaded"] = uploaded
    if uploaded:
        merged["uploaded_at"] = str(merged.get("uploaded_at") or _utcnow_iso())
    else:
        merged.pop("uploaded_at", None)
    if "message" in merged:
        merged["last_message"] = str(merged.get("message") or "")

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
    account.set_extra(extra)
    account.updated_at = _utcnow()
    if session is not None:
        session.add(account)
        if commit:
            session.commit()
            session.refresh(account)
    return state


def _get_db_setting(key: str, default: str) -> str:
    return str(config_store.get(key, default) or default).strip()


def _resolve_db_conninfo() -> str:
    host = _get_db_setting("sub2api_db_host", "127.0.0.1")
    port = _get_db_setting("sub2api_db_port", "5432")
    user = _get_db_setting("sub2api_db_user", "sub2api")
    password = _get_db_setting("sub2api_db_password", "")
    dbname = _get_db_setting("sub2api_db_name", "sub2api")
    sslmode = _get_db_setting("sub2api_db_sslmode", "disable")

    return (
        f"host={host} port={port} dbname={dbname} "
        f"user={user} password={password} sslmode={sslmode} connect_timeout=5"
    )


def _maybe_fetch_rows_via_peer_subprocess(query: str, identity: dict[str, str]) -> list[dict[str, Any]] | None:
    host = _get_db_setting("sub2api_db_host", "127.0.0.1")
    peer_uid = str(os.getenv("SUB2API_DB_PEER_UID") or "").strip()
    peer_gid = str(os.getenv("SUB2API_DB_PEER_GID") or "").strip()
    if not host.startswith("/") or not peer_uid or not peer_gid:
        return None

    script = """
import json
import sys
import psycopg
from psycopg.rows import dict_row

conninfo = sys.argv[1]
query = sys.argv[2]
params = json.loads(sys.argv[3])

with psycopg.connect(conninfo, row_factory=dict_row) as conn:
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall() or []

items = []
for row in rows:
    item = dict(row)
    for key in ("updated_at", "created_at"):
        value = item.get(key)
        if hasattr(value, "isoformat"):
            item[key] = value.isoformat()
    items.append(item)
print(json.dumps(items, ensure_ascii=False))
""".strip()

    result = subprocess.run(
        [
            "setpriv",
            f"--reuid={peer_uid}",
            f"--regid={peer_gid}",
            "--clear-groups",
            sys.executable,
            "-c",
            script,
            _resolve_db_conninfo(),
            query,
            json.dumps(identity, ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"setpriv 退出码 {result.returncode}")

    return json.loads(result.stdout or "[]")


def _fetch_matching_rows(query: str, identity: dict[str, str]) -> list[dict[str, Any]]:
    peer_rows = _maybe_fetch_rows_via_peer_subprocess(query, identity)
    if peer_rows is not None:
        return peer_rows

    with psycopg.connect(_resolve_db_conninfo(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query, identity)
            return cur.fetchall() or []


def _build_probe_identity(account: Any) -> dict[str, str]:
    sync_account = account if getattr(account, "access_token", None) else build_chatgpt_sync_account(account)
    payload = build_sub2api_lookup_payload(sync_account)
    credentials = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else {}
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}

    email = str(extra.get("email") or payload.get("name") or getattr(account, "email", "") or "").strip()
    return {
        "email": email,
        "organization_id": str(credentials.get("organization_id") or "").strip(),
        "chatgpt_account_id": str(credentials.get("chatgpt_account_id") or "").strip(),
        "chatgpt_user_id": str(credentials.get("chatgpt_user_id") or "").strip(),
    }


def _candidate_matches(row: dict[str, Any], identity: dict[str, str]) -> list[str]:
    matches: list[str] = []
    email = identity.get("email", "")
    credentials = row.get("credentials") if isinstance(row.get("credentials"), dict) else {}
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}

    if email and (str(row.get("name") or "").strip() == email or str(extra.get("email") or "").strip() == email):
        matches.append("email")

    organization_id = identity.get("organization_id", "")
    chatgpt_account_id = identity.get("chatgpt_account_id", "")
    if (
        organization_id
        and chatgpt_account_id
        and str(credentials.get("organization_id") or "").strip() == organization_id
        and str(credentials.get("chatgpt_account_id") or "").strip() == chatgpt_account_id
    ):
        matches.append("organization_account")

    chatgpt_user_id = identity.get("chatgpt_user_id", "")
    if chatgpt_user_id and str(credentials.get("chatgpt_user_id") or "").strip() == chatgpt_user_id:
        matches.append("chatgpt_user_id")

    return matches


def _identity_prefers_exact_workspace(identity: dict[str, str]) -> bool:
    return bool(identity.get("organization_id") and identity.get("chatgpt_account_id"))


def _build_probe_query(*, deleted: bool) -> str:
    deleted_clause = "deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"
    return f"""
    SELECT id, name, status, credentials, extra, updated_at, created_at
    FROM accounts
    WHERE {deleted_clause}
      AND platform = 'openai'
      AND type = 'oauth'
      AND (
        (%(email)s <> '' AND (
          name = %(email)s OR COALESCE(extra->>'email', '') = %(email)s
        ))
        OR (
          %(organization_id)s <> ''
          AND %(chatgpt_account_id)s <> ''
          AND COALESCE(credentials->>'organization_id', '') = %(organization_id)s
          AND COALESCE(credentials->>'chatgpt_account_id', '') = %(chatgpt_account_id)s
        )
        OR (
          %(chatgpt_user_id)s <> ''
          AND COALESCE(credentials->>'chatgpt_user_id', '') = %(chatgpt_user_id)s
        )
      )
    ORDER BY updated_at DESC NULLS LAST, id DESC
    LIMIT 10
    """


def _collect_probe_candidates(rows: list[dict[str, Any]], identity: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    exact_candidates: list[dict[str, Any]] = []
    weak_candidates: list[dict[str, Any]] = []

    for row in rows:
        matched_by = _candidate_matches(row, identity)
        if not matched_by:
            continue
        candidate = {
            "id": row.get("id"),
            "name": row.get("name") or "",
            "status": row.get("status") or "",
            "matched_by": matched_by,
            "updated_at": row.get("updated_at").isoformat() if getattr(row.get("updated_at"), "isoformat", None) else row.get("updated_at"),
        }
        candidates.append(candidate)
        if "organization_account" in matched_by:
            exact_candidates.append(candidate)
        else:
            weak_candidates.append(candidate)

    return candidates, exact_candidates, weak_candidates


def probe_chatgpt_sub2api_status(account: Any) -> dict[str, Any]:
    identity = _build_probe_identity(account)
    if not any(identity.values()):
        return {
            "remote_state": "not_found",
            "uploaded": False,
            "matched_by": "",
            "message": "缺少可用于 Sub2API 探测的标识",
            "checked_at": _utcnow_iso(),
        }

    try:
        rows = _fetch_matching_rows(_build_probe_query(deleted=False), identity)
    except Exception as exc:
        return {
            "remote_state": "unreachable",
            "uploaded": False,
            "matched_by": "",
            "message": f"Sub2API 数据库不可用: {exc}",
            "checked_at": _utcnow_iso(),
        }

    candidates, exact_candidates, weak_candidates = _collect_probe_candidates(rows, identity)
    prefers_exact_workspace = _identity_prefers_exact_workspace(identity)
    deleted_exact_candidates: list[dict[str, Any]] = []

    if prefers_exact_workspace and not exact_candidates:
        try:
            _, deleted_exact_candidates, _ = _collect_probe_candidates(
                _fetch_matching_rows(_build_probe_query(deleted=True), identity),
                identity,
            )
        except Exception:
            deleted_exact_candidates = []

    if prefers_exact_workspace:
        if len(exact_candidates) == 1:
            candidate = exact_candidates[0]
            return {
                "remote_state": "exists",
                "uploaded": True,
                "remote_account_id": candidate.get("id"),
                "status": candidate.get("status") or "",
                "matched_by": ", ".join(candidate.get("matched_by") or []),
                "message": f"远端已存在 Sub2API 账号 (#{candidate.get('id')})",
                "checked_at": _utcnow_iso(),
            }
        if len(exact_candidates) > 1:
            return {
                "remote_state": "ambiguous",
                "uploaded": False,
                "matched_by": ", ".join(sorted({item for candidate in exact_candidates for item in candidate["matched_by"]})),
                "message": f"远端匹配到 {len(exact_candidates)} 条精确 Sub2API 记录，已跳过上传",
                "candidate_count": len(exact_candidates),
                "candidates": exact_candidates,
                "checked_at": _utcnow_iso(),
            }
        if deleted_exact_candidates:
            return {
                "remote_state": "deleted_exact_match",
                "uploaded": False,
                "matched_by": ", ".join(sorted({item for candidate in deleted_exact_candidates for item in candidate["matched_by"]})),
                "message": f"远端存在 {len(deleted_exact_candidates)} 条已删除的精确 Sub2API 记录，可重新补传",
                "candidate_count": len(deleted_exact_candidates),
                "candidates": deleted_exact_candidates,
                "checked_at": _utcnow_iso(),
            }
        if weak_candidates:
            return {
                "remote_state": "cross_workspace_only",
                "uploaded": False,
                "matched_by": ", ".join(sorted({item for candidate in weak_candidates for item in candidate["matched_by"]})),
                "message": "仅命中同邮箱/同用户的其他 workspace，可为当前 workspace 补传",
                "candidate_count": len(weak_candidates),
                "candidates": weak_candidates,
                "checked_at": _utcnow_iso(),
            }
        return {
            "remote_state": "not_found",
            "uploaded": False,
            "matched_by": "",
            "message": "远端未发现 Sub2API 账号",
            "checked_at": _utcnow_iso(),
        }

    if not candidates:
        return {
            "remote_state": "not_found",
            "uploaded": False,
            "matched_by": "",
            "message": "远端未发现 Sub2API 账号",
            "checked_at": _utcnow_iso(),
        }

    if len(candidates) > 1:
        return {
            "remote_state": "ambiguous",
            "uploaded": False,
            "matched_by": ", ".join(sorted({item for candidate in candidates for item in candidate["matched_by"]})),
            "message": f"远端匹配到 {len(candidates)} 条 Sub2API 记录，已跳过上传",
            "candidate_count": len(candidates),
            "candidates": candidates,
            "checked_at": _utcnow_iso(),
        }

    candidate = candidates[0]
    return {
        "remote_state": "exists",
        "uploaded": True,
        "remote_account_id": candidate.get("id"),
        "status": candidate.get("status") or "",
        "matched_by": ", ".join(candidate.get("matched_by") or []),
        "message": f"远端已存在 Sub2API 账号 (#{candidate.get('id')})",
        "checked_at": _utcnow_iso(),
    }


def _remote_exists(sync_result: dict[str, Any]) -> bool:
    return str(sync_result.get("remote_state") or "").strip().lower() == "exists"


def _remote_ambiguous(sync_result: dict[str, Any]) -> bool:
    return str(sync_result.get("remote_state") or "").strip().lower() == "ambiguous"


def _local_probe_uploadable(probe: dict[str, Any]) -> bool:
    auth = probe.get("auth") if isinstance(probe.get("auth"), dict) else {}
    return str(auth.get("state") or "").strip() in {"refresh_token_valid", "access_token_valid"}


def _build_upload_failure_state(message: str) -> dict[str, Any]:
    return {
        "remote_state": "not_found",
        "uploaded": False,
        "message": message,
        "checked_at": _utcnow_iso(),
    }


def backfill_chatgpt_account_to_sub2api(
    account: AccountModel,
    *,
    session: Session | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    cached_sync = get_sub2api_sync_state(account)
    remote_state = str(cached_sync.get("remote_state") or "").strip().lower()
    # 只有明确已存在且没有残留候选脏数据时才信缓存；其余情况都重新探测。
    use_cached = (
        bool(cached_sync)
        and remote_state == "exists"
        and not cached_sync.get("candidate_count")
        and not cached_sync.get("candidates")
    )

    initial_sync = cached_sync if use_cached else probe_chatgpt_sub2api_status(account)
    update_account_model_sub2api_sync(account, initial_sync, session=session, commit=False)

    if str(initial_sync.get("remote_state") or "").strip().lower() == "unreachable":
        msg = initial_sync.get("message") or "Sub2API 数据库不可连接"
        results.append({"name": "Sub2API 探测", "ok": False, "msg": msg})
        if session is not None and commit:
            session.commit()
            session.refresh(account)
        return {"ok": False, "uploaded": False, "skipped": False, "message": msg, "results": results}

    if _remote_exists(initial_sync):
        msg = f"远端已存在 ({initial_sync.get('matched_by') or '已命中'})，跳过上传"
        results.append({"name": "Sub2API 探测", "ok": True, "msg": msg})
        if session is not None and commit:
            session.commit()
            session.refresh(account)
        return {"ok": True, "uploaded": False, "skipped": True, "message": msg, "results": results}

    if _remote_ambiguous(initial_sync):
        msg = initial_sync.get("message") or "远端匹配到多条记录，已跳过上传"
        results.append({"name": "Sub2API 探测", "ok": False, "msg": msg})
        if session is not None and commit:
            session.commit()
            session.refresh(account)
        return {"ok": False, "uploaded": False, "skipped": True, "message": msg, "results": results}

    initial_remote_state = str(initial_sync.get("remote_state") or "").strip().lower()
    if initial_remote_state in {"cross_workspace_only", "deleted_exact_match"}:
        msg = initial_sync.get("message") or (
            "仅命中其他 workspace，允许为当前 workspace 补传"
            if initial_remote_state == "cross_workspace_only"
            else "远端存在已删除的精确 Sub2API 记录，可重新补传"
        )
        results.append({"name": "Sub2API 探测", "ok": True, "msg": msg})

    sync_account = build_chatgpt_sync_account(account)
    probe = probe_local_chatgpt_status(sync_account, proxy=None)
    update_account_model_local_probe(account, probe, session=session, commit=False)
    if not _local_probe_uploadable(probe):
        auth = probe.get("auth") if isinstance(probe.get("auth"), dict) else {}
        msg = auth.get("message") or f"本地状态不可上传: {auth.get('state') or 'unknown'}"
        blocked_sync_state = {
            **dict(initial_sync or {}),
            "uploaded": False,
            "message": f"{initial_sync.get('message') or '远端未发现 Sub2API 账号'}；但当前无法补传：{msg}",
            "checked_at": _utcnow_iso(),
        }
        update_account_model_sub2api_sync(account, blocked_sync_state, session=session, commit=False)
        results.append({"name": "本地状态探测", "ok": False, "msg": msg})
        if session is not None and commit:
            session.commit()
            session.refresh(account)
        return {"ok": False, "uploaded": False, "skipped": False, "message": msg, "results": results}

    ok, msg = upload_to_sub2api(sync_account)
    upload_state = probe_chatgpt_sub2api_status(account) if ok else _build_upload_failure_state(msg)
    if ok and str(upload_state.get("remote_state") or "").strip().lower() == "not_found":
        upload_state = {
            **upload_state,
            "message": "上传后远端仍未发现 Sub2API 账号",
        }
    update_account_model_sub2api_sync(account, upload_state, session=session, commit=False)
    results.append({"name": "Sub2API 上传", "ok": ok, "msg": msg})

    if not ok:
        if session is not None and commit:
            session.commit()
            session.refresh(account)
        return {"ok": False, "uploaded": False, "skipped": False, "message": msg, "results": results}

    if not _remote_exists(upload_state):
        verify_msg = upload_state.get("message") or "上传后远端仍未发现 Sub2API 账号"
        results.append({"name": "Sub2API 复核", "ok": False, "msg": verify_msg})
        if session is not None and commit:
            session.commit()
            session.refresh(account)
        return {"ok": False, "uploaded": False, "skipped": False, "message": verify_msg, "results": results}

    verify_msg = f"补传完成，远端账号 #{upload_state.get('remote_account_id')}"
    results.append({"name": "Sub2API 复核", "ok": True, "msg": verify_msg})
    if session is not None and commit:
        session.commit()
        session.refresh(account)
    return {"ok": True, "uploaded": True, "skipped": False, "message": verify_msg, "results": results}
