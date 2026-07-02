from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Iterable

from sqlalchemy import text
from sqlmodel import Session, select

from core.db import AccountListStateModel, AccountModel
from services.chatgpt_account_state import AUTH_INVALID_STATES, classify_chatgpt_capabilities

AUTO_DELETE_REVIVAL_TASK_ID = "icloud_hme_auto_delete"
logger = logging.getLogger(__name__)


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _lower_text(value: Any) -> str:
    return _safe_str(value).lower()


def _parse_subscription_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        return numeric / 1000 if numeric > 1_000_000_000_000 else numeric

    text = _safe_str(value)
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

    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _split_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            items.extend(_split_values(item))
        return {item for item in items if item}
    return {item.strip().lower() for item in str(value).split(",") if item.strip()}


def account_base_query(*, platform: str | None = None, status: Any = None, email: str | None = None):
    query = select(AccountModel)
    platform_value = _safe_str(platform)
    if platform_value:
        query = query.where(AccountModel.platform == platform_value)

    status_values = _split_values(status)
    if len(status_values) == 1:
        query = query.where(AccountModel.status == next(iter(status_values)))
    elif len(status_values) > 1:
        query = query.where(AccountModel.status.in_(sorted(status_values)))

    email_value = _safe_str(email)
    if email_value:
        query = query.where(AccountModel.email.contains(email_value))
    return query


def _extra(account: AccountModel) -> dict[str, Any]:
    try:
        extra = account.get_extra()
    except Exception:
        return {}
    return extra if isinstance(extra, dict) else {}


def _chatgpt_capabilities(account: AccountModel, extra: dict[str, Any]) -> dict[str, Any]:
    capabilities = extra.get("chatgpt_capabilities") if isinstance(extra.get("chatgpt_capabilities"), dict) else {}
    if capabilities:
        return capabilities
    local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else None
    return classify_chatgpt_capabilities(account, local_probe=local_probe)


def _has_refresh_token(account: AccountModel, extra: dict[str, Any]) -> bool:
    return bool(_safe_str(extra.get("refresh_token") or extra.get("refreshToken") or getattr(account, "refresh_token", "")))


def _has_access_token(account: AccountModel, extra: dict[str, Any]) -> bool:
    return bool(
        _safe_str(
            extra.get("access_token")
            or extra.get("accessToken")
            or extra.get("webAccessToken")
            or getattr(account, "access_token", "")
            or account.token
        )
    )


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _normalize_revival_mode(value: Any) -> str:
    mode = _lower_text(value)
    if mode in {"revive_existing", "create_new"}:
        return mode
    return ""


def _normalize_revival_kind(*, source: Any, task_id: Any = "", mode: Any = "") -> str:
    normalized_source = _lower_text(source)
    normalized_task_id = _safe_str(task_id)
    normalized_mode = _normalize_revival_mode(mode)
    if normalized_source == "invalid_account_recheck":
        return "auto_delete_recheck" if normalized_task_id == AUTO_DELETE_REVIVAL_TASK_ID else "invalid_recheck"
    if normalized_source == "custom_email_recheck":
        return "custom_email_recheck_new" if normalized_mode == "create_new" else "custom_email_recheck"
    if normalized_mode == "create_new":
        return "custom_email_recheck_new"
    return normalized_source or "unknown"


def _revival_label(kind: str) -> str:
    labels = {
        "invalid_recheck": "失效测活恢复",
        "auto_delete_recheck": "删前测活恢复",
        "custom_email_recheck": "邮箱测活恢复",
        "custom_email_recheck_new": "邮箱测活新建",
        "unknown": "已恢复",
        "none": "普通",
    }
    return labels.get(kind, "已恢复")


def _revival_state(kind: str) -> str:
    if kind in {"invalid_recheck", "auto_delete_recheck", "custom_email_recheck", "unknown"}:
        return "revived"
    if kind == "custom_email_recheck_new":
        return "recovery_new"
    return "none"


def _build_revival_info(
    *,
    kind: str,
    source: Any = "",
    mode: Any = "",
    at: Any = "",
    task_id: Any = "",
    auth_level: Any = "",
    legacy_inferred: bool = False,
) -> dict[str, Any]:
    normalized_kind = kind or "none"
    normalized_mode = _normalize_revival_mode(mode)
    state = _revival_state(normalized_kind)
    return {
        "state": state,
        "kind": normalized_kind,
        "label": _revival_label(normalized_kind),
        "source": _safe_str(source),
        "mode": normalized_mode,
        "revived": state == "revived",
        "recovery_flow": state in {"revived", "recovery_new"},
        "at": _safe_str(at),
        "task_id": _safe_str(task_id),
        "auth_level": _safe_str(auth_level),
        "legacy_inferred": bool(legacy_inferred),
    }


def account_revival_info(account: AccountModel, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    extra = extra if isinstance(extra, dict) else _extra(account)

    last_marker = _dict_value(extra.get("chatgpt_last_revival"))
    if last_marker:
        mode = _normalize_revival_mode(last_marker.get("mode"))
        kind = _normalize_revival_kind(
            source=last_marker.get("source"),
            task_id=last_marker.get("task_id"),
            mode=mode,
        )
        return _build_revival_info(
            kind=kind,
            source=last_marker.get("source"),
            mode=mode,
            at=last_marker.get("revived_at"),
            task_id=last_marker.get("task_id"),
            auth_level=last_marker.get("auth_level"),
            legacy_inferred=False,
        )

    invalid_recheck = _dict_value(extra.get("chatgpt_invalid_recheck"))
    invalid_recheck_marker = _dict_value(invalid_recheck.get("revival_marker"))
    if invalid_recheck_marker:
        mode = _normalize_revival_mode(invalid_recheck_marker.get("mode"))
        kind = _normalize_revival_kind(
            source=invalid_recheck_marker.get("source"),
            task_id=invalid_recheck_marker.get("task_id"),
            mode=mode,
        )
        return _build_revival_info(
            kind=kind,
            source=invalid_recheck_marker.get("source"),
            mode=mode,
            at=invalid_recheck_marker.get("revived_at"),
            task_id=invalid_recheck_marker.get("task_id"),
            auth_level=invalid_recheck_marker.get("auth_level"),
            legacy_inferred=False,
        )
    if _lower_text(invalid_recheck.get("status")) == "recovered_access_token":
        auth_level = _safe_str(invalid_recheck.get("final_auth_level"))
        if not auth_level:
            if bool(invalid_recheck.get("followup_auth_ok") or invalid_recheck.get("has_refresh_token")):
                auth_level = "refresh_token"
            elif bool(invalid_recheck.get("has_access_token")):
                auth_level = "access_token_only"
        source = _safe_str(invalid_recheck.get("source") or "invalid_account_recheck")
        task_id = _safe_str(invalid_recheck.get("task_id"))
        return _build_revival_info(
            kind=_normalize_revival_kind(source=source, task_id=task_id, mode="revive_existing"),
            source=source,
            mode="revive_existing",
            at=invalid_recheck.get("checked_at") or invalid_recheck.get("revived_at"),
            task_id=task_id,
            auth_level=auth_level,
            legacy_inferred=True,
        )

    custom_recheck = _dict_value(extra.get("chatgpt_custom_email_recheck"))
    custom_recheck_marker = _dict_value(custom_recheck.get("revival_marker"))
    if custom_recheck_marker:
        mode = _normalize_revival_mode(custom_recheck_marker.get("mode"))
        kind = _normalize_revival_kind(
            source=custom_recheck_marker.get("source"),
            task_id=custom_recheck_marker.get("task_id"),
            mode=mode,
        )
        return _build_revival_info(
            kind=kind,
            source=custom_recheck_marker.get("source"),
            mode=mode,
            at=custom_recheck_marker.get("revived_at"),
            task_id=custom_recheck_marker.get("task_id"),
            auth_level=custom_recheck_marker.get("auth_level"),
            legacy_inferred=False,
        )
    if bool(custom_recheck.get("revived_existing_account")) or bool(custom_recheck.get("created_new_account")):
        mode = "revive_existing" if bool(custom_recheck.get("revived_existing_account")) else "create_new"
        auth_level = "refresh_token" if bool(custom_recheck.get("followup_auth_ok") or custom_recheck.get("has_refresh_token")) else ""
        if not auth_level and bool(custom_recheck.get("has_access_token")):
            auth_level = "access_token_only"
        source = _safe_str(custom_recheck.get("source") or "custom_email_recheck")
        task_id = _safe_str(custom_recheck.get("task_id"))
        return _build_revival_info(
            kind=_normalize_revival_kind(source=source, task_id=task_id, mode=mode),
            source=source,
            mode=mode,
            at=custom_recheck.get("checked_at") or custom_recheck.get("revived_at"),
            task_id=task_id,
            auth_level=auth_level,
            legacy_inferred=True,
        )

    return _build_revival_info(kind="none")


def account_revival_kind(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    return _safe_str(account_revival_info(account, extra).get("kind")) or "none"


def account_revival_state(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    return _safe_str(account_revival_info(account, extra).get("state")) or "none"


def account_auth_type(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    if _has_refresh_token(account, extra):
        return "refresh_token"
    if _has_access_token(account, extra):
        return "access_token_only"
    return "unknown"


def _normalize_subscription_type(plan: Any, workspace_scope: Any = "") -> str:
    normalized = _lower_text(plan).replace("-", "_")
    scope = _lower_text(workspace_scope)
    if "enterprise" in normalized:
        return "enterprise"
    if "team" in normalized or "business" in normalized or scope == "business":
        return "team"
    if "pro" in normalized:
        return "pro"
    if "plus" in normalized:
        return "plus"
    if "free" in normalized or scope == "free":
        return "free"
    return "unknown"


def account_subscription_type(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    capabilities = _chatgpt_capabilities(account, extra)
    local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    subscription = local_probe.get("subscription") if isinstance(local_probe.get("subscription"), dict) else {}
    workspace_scope = extra.get("chatgpt_workspace_scope")
    for candidate in (
        capabilities.get("subscription_plan"),
        subscription.get("plan"),
        extra.get("chatgpt_plan_type"),
        extra.get("chatgpt_subscription_plan"),
    ):
        # `workspace_scope=free` only describes the workspace/account scope; it
        # is not a paid-plan source.  Do not let it turn an explicit
        # `subscription_plan=unknown` into `free` before later durable markers
        # such as `chatgpt_plan_type=plus` get a chance to win.
        resolved = _normalize_subscription_type(candidate, "")
        if resolved != "unknown":
            return resolved
    return _normalize_subscription_type("", workspace_scope)


def account_validity(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    if _lower_text(account.status) == "invalid":
        return "invalid"
    extra = extra if isinstance(extra, dict) else _extra(account)
    capabilities = _chatgpt_capabilities(account, extra)
    if _lower_text(capabilities.get("auth_level")) == "invalid":
        return "invalid"
    if _lower_text(capabilities.get("upload_gate")) == "blocked_auth_invalid":
        return "invalid"

    local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    for section_name in ("auth", "codex"):
        section = local_probe.get(section_name) if isinstance(local_probe.get(section_name), dict) else {}
        if _lower_text(section.get("state")) in AUTH_INVALID_STATES:
            return "invalid"
        if int(section.get("http_status") or 0) == 401:
            return "invalid"
    return "valid"


def account_sub2api_state(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    sync_statuses = extra.get("sync_statuses") if isinstance(extra.get("sync_statuses"), dict) else {}
    sub2api = sync_statuses.get("sub2api") if isinstance(sync_statuses.get("sub2api"), dict) else {}
    return _lower_text(sub2api.get("remote_state")) or "unknown"


def account_oaipay_state(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _extra(account)
    sync_statuses = extra.get("sync_statuses") if isinstance(extra.get("sync_statuses"), dict) else {}
    oaipay = sync_statuses.get("oaipay") if isinstance(sync_statuses.get("oaipay"), dict) else {}
    return _lower_text(oaipay.get("remote_state")) or "unknown"


def account_subscription_active_until_timestamp(account: AccountModel, extra: dict[str, Any] | None = None) -> float | None:
    extra = extra if isinstance(extra, dict) else _extra(account)
    local_probe = extra.get("chatgpt_local") if isinstance(extra.get("chatgpt_local"), dict) else {}
    subscription = local_probe.get("subscription") if isinstance(local_probe.get("subscription"), dict) else {}
    for candidate in (
        subscription.get("subscription_active_until"),
        subscription.get("subscription_expires_at_iso"),
        subscription.get("subscription_expires_at"),
        extra.get("subscription_active_until"),
        extra.get("subscription_expires_at"),
        extra.get("chatgpt_subscription_active_until"),
    ):
        timestamp = _parse_subscription_time(candidate)
        if timestamp is not None:
            return timestamp
    return None


def ensure_account_list_state_schema(session: Session) -> None:
    """Create/upgrade the denormalized account list state table for request-time use."""

    session.exec(
        text(
            """
            CREATE TABLE IF NOT EXISTS account_list_state (
                account_id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL DEFAULT '',
                manually_used INTEGER NOT NULL DEFAULT 0,
                auth_type TEXT NOT NULL DEFAULT 'unknown',
                auth_level TEXT NOT NULL DEFAULT '',
                subscription_type TEXT NOT NULL DEFAULT 'unknown',
                account_validity TEXT NOT NULL DEFAULT 'valid',
                sub2api_state TEXT NOT NULL DEFAULT 'unknown',
                revival_state TEXT NOT NULL DEFAULT 'none',
                revival_kind TEXT NOT NULL DEFAULT 'none',
                subscription_active_until TEXT NOT NULL DEFAULT '',
                subscription_active_until_ts REAL,
                source_updated_at TEXT NOT NULL DEFAULT '',
                refreshed_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
    )
    required_columns = {
        "platform": "TEXT NOT NULL DEFAULT ''",
        "manually_used": "INTEGER NOT NULL DEFAULT 0",
        "auth_type": "TEXT NOT NULL DEFAULT 'unknown'",
        "auth_level": "TEXT NOT NULL DEFAULT ''",
        "subscription_type": "TEXT NOT NULL DEFAULT 'unknown'",
        "account_validity": "TEXT NOT NULL DEFAULT 'valid'",
        "sub2api_state": "TEXT NOT NULL DEFAULT 'unknown'",
        "oaipay_state": "TEXT NOT NULL DEFAULT 'unknown'",
        "revival_state": "TEXT NOT NULL DEFAULT 'none'",
        "revival_kind": "TEXT NOT NULL DEFAULT 'none'",
        "subscription_active_until": "TEXT NOT NULL DEFAULT ''",
        "subscription_active_until_ts": "REAL",
        "source_updated_at": "TEXT NOT NULL DEFAULT ''",
        "refreshed_at": "TEXT NOT NULL DEFAULT ''",
    }
    existing_columns: set[str] = set()
    for row in session.exec(text("PRAGMA table_info(account_list_state)")).all():
        try:
            column_name = str(row[1] or "")
        except Exception:
            column_name = ""
        if column_name:
            existing_columns.add(column_name)
    for column_name, ddl in required_columns.items():
        if column_name in existing_columns:
            continue
        session.exec(text(f"ALTER TABLE account_list_state ADD COLUMN {column_name} {ddl}"))
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_platform ON account_list_state(platform)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_manually_used ON account_list_state(manually_used)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_auth_type ON account_list_state(auth_type)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_subscription_type ON account_list_state(subscription_type)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_account_validity ON account_list_state(account_validity)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_sub2api_state ON account_list_state(sub2api_state)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_oaipay_state ON account_list_state(oaipay_state)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_revival_state ON account_list_state(revival_state)",
        "CREATE INDEX IF NOT EXISTS idx_account_list_state_subscription_active_until_ts ON account_list_state(subscription_active_until_ts)",
    ):
        session.exec(text(index_sql))


def _normalize_account_list_state_ids(account_ids: Any) -> list[int]:
    if account_ids is None:
        return []
    if isinstance(account_ids, (str, bytes)) or not isinstance(account_ids, Iterable):
        raw_items = [account_ids]
    else:
        raw_items = list(account_ids)
    ids: list[int] = []
    seen: set[int] = set()
    for value in raw_items:
        try:
            account_id = int(value or 0)
        except Exception:
            continue
        if account_id <= 0 or account_id in seen:
            continue
        seen.add(account_id)
        ids.append(account_id)
    return ids


def _account_list_state_target_where(
    *,
    account_ids: Any = None,
    stale_only: bool = False,
    platform: Any = None,
) -> str:
    terms = ["1 = 1"]
    ids = _normalize_account_list_state_ids(account_ids)
    if ids:
        terms.append("id IN (" + ",".join(str(account_id) for account_id in ids) + ")")
    platform_value = _safe_str(platform)
    if platform_value:
        terms.append("platform = '" + platform_value.replace("'", "''") + "'")
    if stale_only:
        terms.append(
            """
            (
                NOT EXISTS (
                    SELECT 1
                    FROM account_list_state AS state
                    WHERE state.account_id = accounts.id
                )
                OR coalesce((
                    SELECT state.source_updated_at
                    FROM account_list_state AS state
                    WHERE state.account_id = accounts.id
                ), '') != CAST(accounts.updated_at AS TEXT)
            )
            """
        )
    return " AND ".join(terms)


def _looks_like_sql_session(session: Any) -> bool:
    if session is None:
        return False
    if type(session).__module__.startswith("unittest.mock"):
        return False
    exec_fn = getattr(session, "exec", None)
    return callable(exec_fn)


def _log_account_list_state_sync_skip(action: str, account_ids: list[int], exc: Exception) -> None:
    logger.warning(
        "Account list-state %s skipped account_ids=%s error=%s",
        action,
        account_ids[:20],
        exc,
        exc_info=True,
    )


def refresh_account_list_state(
    session: Session,
    *,
    account_ids: Any = None,
    stale_only: bool = False,
    platform: Any = None,
    cleanup_orphans: bool = True,
    commit: bool = True,
) -> int:
    """Refresh list-derived state from accounts with SQL-only extraction.

    The table intentionally stores only non-secret summaries used by list
    filters/sorts.  Token values are read only as presence checks and are never
    persisted.
    """

    ensure_account_list_state_schema(session)
    session.flush()
    target_where = _account_list_state_target_where(
        account_ids=account_ids,
        stale_only=stale_only,
        platform=platform,
    )
    target_count_row = session.exec(text(f"SELECT COUNT(*) FROM accounts WHERE {target_where}")).one()
    try:
        target_count_value = target_count_row[0]
    except Exception:
        target_count_value = target_count_row
    target_count = int(target_count_value or 0)
    if target_count > 0:
        session.exec(
            text(
                """
            WITH account_json AS (
                SELECT
                    id AS account_id,
                    platform,
                    status,
                    token,
                    CAST(updated_at AS TEXT) AS source_updated_at,
                    CASE WHEN json_valid(extra_json) THEN extra_json ELSE '{}' END AS extra
                FROM accounts
                WHERE __ACCOUNT_LIST_STATE_TARGET_WHERE__
            ),
            extracted AS (
                SELECT
                    account_id,
                    platform,
                    status,
                    token,
                    source_updated_at,
                    extra,
                    lower(trim(status)) AS account_status,
                    trim(coalesce(
                        json_extract(extra, '$.refresh_token'),
                        json_extract(extra, '$.refreshToken'),
                        ''
                    )) AS refresh_token_value,
                    trim(coalesce(
                        json_extract(extra, '$.access_token'),
                        json_extract(extra, '$.accessToken'),
                        json_extract(extra, '$.webAccessToken'),
                        token,
                        ''
                    )) AS access_token_value,
                    CASE
                        WHEN lower(trim(CAST(coalesce(json_extract(extra, '$.manually_used'), '') AS TEXT)))
                             IN ('1', 'true', 'yes', 'on', 'used')
                        THEN 1
                        ELSE 0
                    END AS manually_used,
                    lower(trim(coalesce(
                        json_extract(extra, '$.chatgpt_capabilities.auth_level'),
                        json_extract(extra, '$.auth_level'),
                        ''
                    ))) AS auth_level,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_capabilities.upload_gate'), ''))) AS upload_gate,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_local.auth.state'), ''))) AS auth_state,
                    CAST(coalesce(json_extract(extra, '$.chatgpt_local.auth.http_status'), 0) AS INTEGER) AS auth_http_status,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_local.codex.state'), ''))) AS codex_state,
                    CAST(coalesce(json_extract(extra, '$.chatgpt_local.codex.http_status'), 0) AS INTEGER) AS codex_http_status,
                    replace(lower(trim(coalesce(json_extract(extra, '$.chatgpt_capabilities.subscription_plan'), ''))), '-', '_') AS cap_subscription_plan,
                    replace(lower(trim(coalesce(json_extract(extra, '$.chatgpt_local.subscription.plan'), ''))), '-', '_') AS local_subscription_plan,
                    replace(lower(trim(coalesce(json_extract(extra, '$.chatgpt_plan_type'), ''))), '-', '_') AS plan_type,
                    replace(lower(trim(coalesce(json_extract(extra, '$.chatgpt_subscription_plan'), ''))), '-', '_') AS extra_subscription_plan,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_workspace_scope'), ''))) AS workspace_scope,
                    lower(trim(coalesce(json_extract(extra, '$.sync_statuses.sub2api.remote_state'), ''))) AS sub2api_remote_state,
                    lower(trim(coalesce(json_extract(extra, '$.sync_statuses.oaipay.remote_state'), ''))) AS oaipay_remote_state,
                    coalesce(
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_local.subscription.subscription_active_until') AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_local.subscription.subscription_expires_at_iso') AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_local.subscription.subscription_expires_at') AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.subscription_active_until') AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.subscription_expires_at') AS TEXT)), ''),
                        nullif(trim(CAST(json_extract(extra, '$.chatgpt_subscription_active_until') AS TEXT)), ''),
                        ''
                    ) AS raw_subscription_active_until,
                    CASE WHEN json_type(extra, '$.chatgpt_last_revival') = 'object' THEN 1 ELSE 0 END AS last_revival_present,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_revival.source'), ''))) AS last_revival_source,
                    trim(coalesce(json_extract(extra, '$.chatgpt_last_revival.task_id'), '')) AS last_revival_task_id,
                    CASE
                        WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_revival.mode'), '')))
                             IN ('revive_existing', 'create_new')
                        THEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_last_revival.mode'), '')))
                        ELSE ''
                    END AS last_revival_mode,
                    CASE WHEN json_type(extra, '$.chatgpt_invalid_recheck.revival_marker') = 'object' THEN 1 ELSE 0 END AS invalid_marker_present,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.revival_marker.source'), ''))) AS invalid_marker_source,
                    trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.revival_marker.task_id'), '')) AS invalid_marker_task_id,
                    CASE
                        WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.revival_marker.mode'), '')))
                             IN ('revive_existing', 'create_new')
                        THEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.revival_marker.mode'), '')))
                        ELSE ''
                    END AS invalid_marker_mode,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.status'), ''))) AS invalid_recheck_status,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.source'), 'invalid_account_recheck'))) AS invalid_recheck_source,
                    trim(coalesce(json_extract(extra, '$.chatgpt_invalid_recheck.task_id'), '')) AS invalid_recheck_task_id,
                    CASE WHEN json_type(extra, '$.chatgpt_custom_email_recheck.revival_marker') = 'object' THEN 1 ELSE 0 END AS custom_marker_present,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.revival_marker.source'), ''))) AS custom_marker_source,
                    trim(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.revival_marker.task_id'), '')) AS custom_marker_task_id,
                    CASE
                        WHEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.revival_marker.mode'), '')))
                             IN ('revive_existing', 'create_new')
                        THEN lower(trim(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.revival_marker.mode'), '')))
                        ELSE ''
                    END AS custom_marker_mode,
                    CASE
                        WHEN lower(trim(CAST(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.revived_existing_account'), '') AS TEXT)))
                             IN ('1', 'true', 'yes', 'on')
                        THEN 1
                        ELSE 0
                    END AS custom_revived_existing,
                    CASE
                        WHEN lower(trim(CAST(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.created_new_account'), '') AS TEXT)))
                             IN ('1', 'true', 'yes', 'on')
                        THEN 1
                        ELSE 0
                    END AS custom_created_new,
                    lower(trim(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.source'), 'custom_email_recheck'))) AS custom_recheck_source,
                    trim(coalesce(json_extract(extra, '$.chatgpt_custom_email_recheck.task_id'), '')) AS custom_recheck_task_id
                FROM account_json
            ),
            revival_inputs AS (
                SELECT
                    *,
                    CASE
                        WHEN last_revival_present = 1 THEN 1
                        WHEN invalid_marker_present = 1 THEN 1
                        WHEN invalid_recheck_status = 'recovered_access_token' THEN 1
                        WHEN custom_marker_present = 1 THEN 1
                        WHEN custom_revived_existing = 1 OR custom_created_new = 1 THEN 1
                        ELSE 0
                    END AS has_revival,
                    CASE
                        WHEN last_revival_present = 1 THEN last_revival_source
                        WHEN invalid_marker_present = 1 THEN invalid_marker_source
                        WHEN invalid_recheck_status = 'recovered_access_token' THEN invalid_recheck_source
                        WHEN custom_marker_present = 1 THEN custom_marker_source
                        WHEN custom_revived_existing = 1 OR custom_created_new = 1 THEN custom_recheck_source
                        ELSE ''
                    END AS revival_source,
                    CASE
                        WHEN last_revival_present = 1 THEN last_revival_task_id
                        WHEN invalid_marker_present = 1 THEN invalid_marker_task_id
                        WHEN invalid_recheck_status = 'recovered_access_token' THEN invalid_recheck_task_id
                        WHEN custom_marker_present = 1 THEN custom_marker_task_id
                        WHEN custom_revived_existing = 1 OR custom_created_new = 1 THEN custom_recheck_task_id
                        ELSE ''
                    END AS revival_task_id,
                    CASE
                        WHEN last_revival_present = 1 THEN last_revival_mode
                        WHEN invalid_marker_present = 1 THEN invalid_marker_mode
                        WHEN invalid_recheck_status = 'recovered_access_token' THEN 'revive_existing'
                        WHEN custom_marker_present = 1 THEN custom_marker_mode
                        WHEN custom_created_new = 1 THEN 'create_new'
                        WHEN custom_revived_existing = 1 THEN 'revive_existing'
                        ELSE ''
                    END AS revival_mode
                FROM extracted
            ),
            derived AS (
                SELECT
                    *,
                    CASE
                        WHEN refresh_token_value != '' THEN 'refresh_token'
                        WHEN access_token_value != '' THEN 'access_token_only'
                        ELSE 'unknown'
                    END AS derived_auth_type,
                    CASE
                        WHEN account_status = 'invalid'
                            OR auth_level = 'invalid'
                            OR upload_gate = 'blocked_auth_invalid'
                            OR auth_state IN (
                                'refresh_token_invalidated',
                                'access_token_invalidated',
                                'unauthorized',
                                'account_deactivated',
                                'banned_like'
                            )
                            OR codex_state IN (
                                'refresh_token_invalidated',
                                'access_token_invalidated',
                                'unauthorized',
                                'account_deactivated',
                                'banned_like'
                            )
                            OR auth_http_status = 401
                            OR codex_http_status = 401
                        THEN 'invalid'
                        ELSE 'valid'
                    END AS derived_account_validity,
                    CASE
                        WHEN cap_subscription_plan LIKE '%enterprise%' THEN 'enterprise'
                        WHEN cap_subscription_plan LIKE '%team%' OR cap_subscription_plan LIKE '%business%' THEN 'team'
                        WHEN cap_subscription_plan LIKE '%pro%' THEN 'pro'
                        WHEN cap_subscription_plan LIKE '%plus%' THEN 'plus'
                        WHEN cap_subscription_plan LIKE '%free%' THEN 'free'
                        WHEN local_subscription_plan LIKE '%enterprise%' THEN 'enterprise'
                        WHEN local_subscription_plan LIKE '%team%' OR local_subscription_plan LIKE '%business%' THEN 'team'
                        WHEN local_subscription_plan LIKE '%pro%' THEN 'pro'
                        WHEN local_subscription_plan LIKE '%plus%' THEN 'plus'
                        WHEN local_subscription_plan LIKE '%free%' THEN 'free'
                        WHEN plan_type LIKE '%enterprise%' THEN 'enterprise'
                        WHEN plan_type LIKE '%team%' OR plan_type LIKE '%business%' THEN 'team'
                        WHEN plan_type LIKE '%pro%' THEN 'pro'
                        WHEN plan_type LIKE '%plus%' THEN 'plus'
                        WHEN plan_type LIKE '%free%' THEN 'free'
                        WHEN extra_subscription_plan LIKE '%enterprise%' THEN 'enterprise'
                        WHEN extra_subscription_plan LIKE '%team%' OR extra_subscription_plan LIKE '%business%' THEN 'team'
                        WHEN extra_subscription_plan LIKE '%pro%' THEN 'pro'
                        WHEN extra_subscription_plan LIKE '%plus%' THEN 'plus'
                        WHEN extra_subscription_plan LIKE '%free%' THEN 'free'
                        WHEN workspace_scope = 'business' THEN 'team'
                        WHEN workspace_scope = 'free' THEN 'free'
                        ELSE 'unknown'
                    END AS derived_subscription_type,
                    CASE
                        WHEN has_revival = 0 THEN 'none'
                        WHEN revival_source = 'invalid_account_recheck' THEN
                            CASE
                                WHEN revival_task_id = 'icloud_hme_auto_delete' THEN 'auto_delete_recheck'
                                ELSE 'invalid_recheck'
                            END
                        WHEN revival_source = 'custom_email_recheck' THEN
                            CASE
                                WHEN revival_mode = 'create_new' THEN 'custom_email_recheck_new'
                                ELSE 'custom_email_recheck'
                            END
                        WHEN revival_mode = 'create_new' THEN 'custom_email_recheck_new'
                        WHEN revival_source != '' THEN revival_source
                        ELSE 'unknown'
                    END AS derived_revival_kind
                FROM revival_inputs
            ),
            final_rows AS (
                SELECT
                    account_id,
                    platform,
                    manually_used,
                    derived_auth_type AS auth_type,
                    auth_level,
                    derived_subscription_type AS subscription_type,
                    derived_account_validity AS account_validity,
                    CASE WHEN sub2api_remote_state != '' THEN sub2api_remote_state ELSE 'unknown' END AS sub2api_state,
                    CASE WHEN oaipay_remote_state != '' THEN oaipay_remote_state ELSE 'unknown' END AS oaipay_state,
                    CASE
                        WHEN derived_revival_kind IN ('invalid_recheck', 'auto_delete_recheck', 'custom_email_recheck', 'unknown') THEN 'revived'
                        WHEN derived_revival_kind = 'custom_email_recheck_new' THEN 'recovery_new'
                        ELSE 'none'
                    END AS revival_state,
                    derived_revival_kind AS revival_kind,
                    raw_subscription_active_until AS subscription_active_until,
                    CASE
                        WHEN raw_subscription_active_until = '' THEN NULL
                        WHEN raw_subscription_active_until GLOB '[0-9]*'
                             AND raw_subscription_active_until NOT GLOB '*[^0-9.]*'
                        THEN
                            CASE
                                WHEN CAST(raw_subscription_active_until AS REAL) <= 0 THEN NULL
                                WHEN CAST(raw_subscription_active_until AS REAL) > 1000000000000
                                THEN CAST(raw_subscription_active_until AS REAL) / 1000
                                ELSE CAST(raw_subscription_active_until AS REAL)
                            END
                        ELSE CAST(strftime('%s', replace(raw_subscription_active_until, 'Z', '+00:00')) AS REAL)
                    END AS subscription_active_until_ts,
                    source_updated_at,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS refreshed_at
                FROM derived
            )
            INSERT INTO account_list_state (
                account_id,
                platform,
                manually_used,
                auth_type,
                auth_level,
                subscription_type,
                account_validity,
                sub2api_state,
                oaipay_state,
                revival_state,
                revival_kind,
                subscription_active_until,
                subscription_active_until_ts,
                source_updated_at,
                refreshed_at
            )
            SELECT
                account_id,
                platform,
                manually_used,
                auth_type,
                auth_level,
                subscription_type,
                account_validity,
                sub2api_state,
                oaipay_state,
                revival_state,
                revival_kind,
                subscription_active_until,
                subscription_active_until_ts,
                source_updated_at,
                refreshed_at
            FROM final_rows
            WHERE 1 = 1
            ON CONFLICT(account_id) DO UPDATE SET
                platform = excluded.platform,
                manually_used = excluded.manually_used,
                auth_type = excluded.auth_type,
                auth_level = excluded.auth_level,
                subscription_type = excluded.subscription_type,
                account_validity = excluded.account_validity,
                sub2api_state = excluded.sub2api_state,
                oaipay_state = excluded.oaipay_state,
                revival_state = excluded.revival_state,
                revival_kind = excluded.revival_kind,
                subscription_active_until = excluded.subscription_active_until,
                subscription_active_until_ts = excluded.subscription_active_until_ts,
                source_updated_at = excluded.source_updated_at,
                refreshed_at = excluded.refreshed_at
            """.replace("__ACCOUNT_LIST_STATE_TARGET_WHERE__", target_where)
        )
    )
    if cleanup_orphans:
        session.exec(
            text(
                """
                DELETE FROM account_list_state
                WHERE account_id NOT IN (SELECT id FROM accounts)
                """
            )
        )
    if commit:
        session.commit()
    return target_count


def refresh_stale_account_list_state(session: Session, *, platform: Any = None, commit: bool = True) -> int:
    """Refresh only missing/stale derived list-state rows.

    A row is stale when its cached ``source_updated_at`` differs from the
    current ``accounts.updated_at`` text.  This keeps derived filters/sorts
    accurate without rebuilding the whole cache on every list request.
    """

    return refresh_account_list_state(
        session,
        stale_only=True,
        platform=platform,
        cleanup_orphans=True,
        commit=commit,
    )


def upsert_account_list_state_for_account_ids(
    session: Session,
    account_ids: Any,
    *,
    commit: bool = True,
) -> int:
    """Synchronize derived list state for known account write points."""

    ids = _normalize_account_list_state_ids(account_ids)
    if not ids or not _looks_like_sql_session(session):
        return 0
    try:
        return refresh_account_list_state(
            session,
            account_ids=ids,
            cleanup_orphans=False,
            commit=commit,
        )
    except Exception as exc:
        _log_account_list_state_sync_skip("upsert", ids, exc)
        return 0


def delete_account_list_state_for_account_ids(
    session: Session,
    account_ids: Any,
    *,
    commit: bool = True,
) -> int:
    """Remove derived list-state rows for deleted accounts."""

    ids = _normalize_account_list_state_ids(account_ids)
    if not ids or not _looks_like_sql_session(session):
        return 0
    try:
        ensure_account_list_state_schema(session)
        session.exec(
            text(
                "DELETE FROM account_list_state WHERE account_id IN ("
                + ",".join(str(account_id) for account_id in ids)
                + ")"
            )
        )
        if commit:
            session.commit()
        return len(ids)
    except Exception as exc:
        _log_account_list_state_sync_skip("delete", ids, exc)
        return 0


def normalize_account_sort_order(value: Any) -> str:
    text = _lower_text(value)
    if text in {"asc", "ascend", "ascending"}:
        return "asc"
    if text in {"desc", "descend", "descending"}:
        return "desc"
    return ""


def should_sort_account_rows(sort_by: Any, sort_order: Any) -> bool:
    return _lower_text(sort_by) == "subscription_active_until" and bool(normalize_account_sort_order(sort_order))


def should_use_account_list_state(
    *,
    manually_used: bool | None = None,
    auth_type: Any = None,
    subscription_type: Any = None,
    account_validity_filter: Any = None,
    sub2api_state: Any = None,
    oaipay_state: Any = None,
    revival_state: Any = None,
    sort_by: Any = None,
    sort_order: Any = None,
) -> bool:
    return any(
        [
            manually_used is not None,
            bool(_split_values(auth_type)),
            bool(_split_values(subscription_type)),
            bool(_split_values(account_validity_filter)),
            bool(_split_values(sub2api_state)),
            bool(_split_values(oaipay_state)),
            bool(_split_values(revival_state)),
            should_sort_account_rows(sort_by, sort_order),
        ]
    )


def apply_account_list_state_filters(
    query: Any,
    *,
    manually_used: bool | None = None,
    auth_type: Any = None,
    subscription_type: Any = None,
    account_validity_filter: Any = None,
    sub2api_state: Any = None,
    oaipay_state: Any = None,
    revival_state: Any = None,
) -> Any:
    if manually_used is not None:
        query = query.where(AccountListStateModel.manually_used == manually_used)

    auth_types = _split_values(auth_type)
    if auth_types:
        query = query.where(AccountListStateModel.auth_type.in_(sorted(auth_types)))

    subscription_types = _split_values(subscription_type)
    if subscription_types:
        query = query.where(AccountListStateModel.subscription_type.in_(sorted(subscription_types)))

    validity_values = _split_values(account_validity_filter)
    if validity_values:
        query = query.where(AccountListStateModel.account_validity.in_(sorted(validity_values)))

    sub2api_states = _split_values(sub2api_state)
    if sub2api_states:
        query = query.where(AccountListStateModel.sub2api_state.in_(sorted(sub2api_states)))

    oaipay_states = _split_values(oaipay_state)
    if oaipay_states:
        query = query.where(AccountListStateModel.oaipay_state.in_(sorted(oaipay_states)))

    revival_states = _split_values(revival_state)
    if revival_states:
        query = query.where(AccountListStateModel.revival_state.in_(sorted(revival_states)))

    return query


def apply_account_list_state_sort(
    query: Any,
    *,
    sort_by: Any = None,
    sort_order: Any = None,
) -> Any:
    if not should_sort_account_rows(sort_by, sort_order):
        return query.order_by(AccountModel.id.desc())

    timestamp_is_empty = AccountListStateModel.subscription_active_until_ts.is_(None)
    if normalize_account_sort_order(sort_order) == "desc":
        return query.order_by(
            timestamp_is_empty.asc(),
            AccountListStateModel.subscription_active_until_ts.desc(),
            AccountModel.id.desc(),
        )
    return query.order_by(
        timestamp_is_empty.asc(),
        AccountListStateModel.subscription_active_until_ts.asc(),
        AccountModel.id.desc(),
    )


def sort_account_rows(rows: Iterable[AccountModel], *, sort_by: Any = None, sort_order: Any = None) -> list[AccountModel]:
    items = list(rows)
    if not should_sort_account_rows(sort_by, sort_order):
        return items

    reverse = normalize_account_sort_order(sort_order) == "desc"

    def sort_key(row: AccountModel) -> tuple[int, float]:
        timestamp = account_subscription_active_until_timestamp(row)
        if timestamp is None:
            return (1, 0)
        return (0, -timestamp if reverse else timestamp)

    return sorted(items, key=sort_key)


def filter_account_rows(
    rows: Iterable[AccountModel],
    *,
    manually_used: bool | None = None,
    auth_type: Any = None,
    subscription_type: Any = None,
    account_validity_filter: Any = None,
    sub2api_state: Any = None,
    oaipay_state: Any = None,
    revival_state: Any = None,
) -> list[AccountModel]:
    auth_types = _split_values(auth_type)
    subscription_types = _split_values(subscription_type)
    validity_values = _split_values(account_validity_filter)
    sub2api_states = _split_values(sub2api_state)
    oaipay_states = _split_values(oaipay_state)
    revival_states = _split_values(revival_state)

    filtered: list[AccountModel] = []
    for row in rows:
        extra = _extra(row)
        if manually_used is not None and bool(extra.get("manually_used")) is not manually_used:
            continue
        if auth_types and account_auth_type(row, extra) not in auth_types:
            continue
        if subscription_types and account_subscription_type(row, extra) not in subscription_types:
            continue
        if validity_values and account_validity(row, extra) not in validity_values:
            continue
        if sub2api_states and account_sub2api_state(row, extra) not in sub2api_states:
            continue
        if oaipay_states and account_oaipay_state(row, extra) not in oaipay_states:
            continue
        if revival_states and account_revival_state(row, extra) not in revival_states:
            continue
        filtered.append(row)
    return filtered
