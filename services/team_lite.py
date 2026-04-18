from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from sqlalchemy import or_
from sqlmodel import Session, select

from core.config_store import config_store
from core.db import AccountModel, engine as account_engine
from services.team_embedded_backend import team_embedded_backend


DEFAULT_TEAM_DB_CANDIDATES = [
    "/runtime/team_manage.db",
    "/team-manage-seed-data/team_manage.db",
    "/root/.openclaw/workspace/team-manage/data/team_manage.db",
]


class TeamLiteService:
    LIVE_MEMBER_CACHE_TTL_SECONDS = 15
    LIVE_MEMBER_FETCH_WORKERS = 6

    def __init__(self) -> None:
        self._live_member_cache: dict[int, dict[str, Any]] = {}
        self._live_member_cache_lock = threading.Lock()

    def _default_db_path(self) -> str:
        for candidate in DEFAULT_TEAM_DB_CANDIDATES:
            if os.path.exists(candidate):
                return candidate
        return DEFAULT_TEAM_DB_CANDIDATES[0]

    def _settings(self) -> dict[str, str]:
        default_db_path = self._default_db_path()
        return {
            "team_manager_db_path": str(
                config_store.get("team_manager_db_path", default_db_path) or default_db_path
            ).strip(),
        }

    def get_settings(self) -> dict[str, str]:
        return self._settings()

    def update_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        current = self._settings()
        safe = {
            "team_manager_db_path": str(
                data.get("team_manager_db_path", current["team_manager_db_path"]) or current["team_manager_db_path"]
            ).strip(),
        }
        config_store.set_many(safe)
        return {"ok": True, "settings": self._settings()}

    def _connect_db(self) -> sqlite3.Connection:
        db_path = self._settings().get("team_manager_db_path") or self._default_db_path()
        if not db_path:
            raise RuntimeError("未配置 Team Manager 数据库路径")
        if not os.path.exists(db_path):
            raise RuntimeError(f"Team Manager 数据库不存在: {db_path}")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _normalize_workspace_scope(value: str) -> str:
        normalized = str(value or "").strip().lower().replace("-", "_")
        if normalized in {"business", "team", "enterprise", "workspace"}:
            return "business"
        if normalized in {"free", "personal", "personal_free"}:
            return "free"
        return ""

    @staticmethod
    def _parse_account_extra(raw_value: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw_value or "{}")
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _find_existing_team(self, *, account_id: str = "", email: str = "") -> dict[str, Any] | None:
        normalized_account_id = str(account_id or "").strip()
        normalized_email = str(email or "").strip().lower()
        if not normalized_account_id and not normalized_email:
            return None

        where_parts: list[str] = []
        params: list[Any] = []
        if normalized_account_id:
            where_parts.append("account_id = ?")
            params.append(normalized_account_id)
        if normalized_email:
            where_parts.append("lower(email) = ?")
            params.append(normalized_email)

        where_clause = " OR ".join(where_parts)
        with self._connect_db() as conn:
            row = conn.execute(
                f"""
                SELECT id, email, account_id, team_name, status
                FROM teams
                WHERE {where_clause}
                ORDER BY id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"] or 0),
            "email": str(row["email"] or ""),
            "account_id": str(row["account_id"] or ""),
            "team_name": str(row["team_name"] or ""),
            "status": str(row["status"] or ""),
        }

    def _match_source_account(self, team_email: str, team_account_id: str, candidates: list[AccountModel]) -> dict[str, Any]:
        normalized_email = str(team_email or "").strip().lower()
        normalized_account_id = str(team_account_id or "").strip()
        ranked: list[tuple[int, AccountModel, dict[str, Any], str]] = []

        for account in candidates:
            extra = self._parse_account_extra(getattr(account, "extra_json", "{}"))
            scope = self._normalize_workspace_scope(str(extra.get("chatgpt_workspace_scope") or ""))
            if scope != "business":
                continue
            score = 0
            if normalized_account_id and str(getattr(account, "user_id", "") or "").strip() == normalized_account_id:
                score += 100
            if normalized_email and str(getattr(account, "email", "") or "").strip().lower() == normalized_email:
                score += 10
            if score <= 0:
                continue
            ranked.append((score, account, extra, scope))

        if not ranked:
            return {}

        ranked.sort(
            key=lambda item: (
                item[0],
                str(getattr(item[1], "created_at", "") or ""),
                int(getattr(item[1], "id", 0) or 0),
            ),
            reverse=True,
        )
        _, account, extra, scope = ranked[0]
        return {
            "account_db_id": int(getattr(account, "id", 0) or 0),
            "email": str(getattr(account, "email", "") or ""),
            "account_id": str(getattr(account, "user_id", "") or ""),
            "status": str(getattr(account, "status", "") or ""),
            "workspace_scope": scope,
            "workspace_label": str(extra.get("chatgpt_workspace_label") or scope or ""),
            "workspace_id": str(extra.get("workspace_id") or ""),
            "has_refresh_token": bool(str(extra.get("refresh_token") or "").strip()),
        }

    def _build_team_source_map(self, team_refs: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        normalized_refs = [item for item in team_refs if int(item.get("id") or 0) > 0]
        if not normalized_refs:
            return {}

        emails = {
            str(item.get("email") or "").strip().lower()
            for item in normalized_refs
            if str(item.get("email") or "").strip()
        }
        account_ids = {
            str(item.get("account_id") or "").strip()
            for item in normalized_refs
            if str(item.get("account_id") or "").strip()
        }
        if not emails and not account_ids:
            return {}

        with Session(account_engine) as session:
            stmt = select(AccountModel).where(AccountModel.platform == "chatgpt")
            filters = []
            if emails:
                filters.append(AccountModel.email.in_(sorted(emails)))
            if account_ids:
                filters.append(AccountModel.user_id.in_(sorted(account_ids)))
            if filters:
                stmt = stmt.where(or_(*filters))
            candidates = list(session.exec(stmt).all())

        source_map: dict[int, dict[str, Any]] = {}
        for item in normalized_refs:
            source_map[int(item["id"])] = self._match_source_account(
                str(item.get("email") or ""),
                str(item.get("account_id") or ""),
                candidates,
            )
        return source_map

    def _get_account_for_team_import(self, account_row_id: int) -> AccountModel | None:
        with Session(account_engine) as session:
            return session.get(AccountModel, int(account_row_id))

    def _build_team_import_payload_from_account(self, account: AccountModel) -> dict[str, Any]:
        if not account or str(getattr(account, "platform", "") or "").strip().lower() != "chatgpt":
            raise RuntimeError("只支持从 ChatGPT 账号导入 Team")

        extra = self._parse_account_extra(getattr(account, "extra_json", "{}"))
        workspace_scope = self._normalize_workspace_scope(str(extra.get("chatgpt_workspace_scope") or ""))
        if workspace_scope != "business":
            raise RuntimeError("只有 business 工作空间账号才能设为 Team 母号")

        email = str(getattr(account, "email", "") or "").strip()
        account_id = str(getattr(account, "user_id", "") or extra.get("account_id") or "").strip()
        access_token = str(extra.get("access_token") or getattr(account, "token", "") or "").strip()
        refresh_token = str(extra.get("refresh_token") or "").strip()
        session_token = str(extra.get("session_token") or "").strip()
        client_id = str(extra.get("client_id") or "app_EMoamEEZ73f0CkXaXp7hrann").strip()

        if not any((access_token, refresh_token, session_token)):
            raise RuntimeError("该账号缺少可用于 Team 导入的凭证")

        return {
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "session_token": session_token,
            "client_id": client_id,
            "workspace_scope": workspace_scope,
            "workspace_label": str(extra.get("chatgpt_workspace_label") or workspace_scope or ""),
            "account_row_id": int(getattr(account, "id", 0) or 0),
        }

    def _get_team_db_detail(self, team_id: int) -> dict[str, Any] | None:
        with self._connect_db() as conn:
            team_row = conn.execute(
                """
                SELECT
                  id,
                  email,
                  account_id,
                  team_name,
                  plan_type,
                  subscription_plan,
                  expires_at,
                  current_members,
                  max_members,
                  status,
                  account_role,
                  device_code_auth_enabled,
                  error_count,
                  last_sync,
                  created_at,
                  client_id
                FROM teams
                WHERE id = ?
                """,
                [int(team_id)],
            ).fetchone()
            if not team_row:
                return None

            account_rows = conn.execute(
                """
                SELECT id, team_id, account_id, account_name, is_primary, created_at
                FROM team_accounts
                WHERE team_id = ?
                ORDER BY is_primary DESC, id ASC
                """,
                [int(team_id)],
            ).fetchall()

        team_accounts = []
        for row in account_rows:
            team_accounts.append(
                {
                    "id": int(row["id"] or 0),
                    "team_id": int(row["team_id"] or 0),
                    "account_id": str(row["account_id"] or ""),
                    "account_name": str(row["account_name"] or ""),
                    "is_primary": bool(row["is_primary"]),
                    "created_at": str(row["created_at"] or ""),
                }
            )

        primary_account = next((item for item in team_accounts if item.get("is_primary")), None)
        return {
            "id": int(team_row["id"] or 0),
            "email": str(team_row["email"] or ""),
            "account_id": str(team_row["account_id"] or ""),
            "team_name": str(team_row["team_name"] or ""),
            "plan_type": str(team_row["plan_type"] or ""),
            "subscription_plan": str(team_row["subscription_plan"] or ""),
            "expires_at": str(team_row["expires_at"] or ""),
            "db_current_members": int(team_row["current_members"] or 0),
            "max_members": int(team_row["max_members"] or 0),
            "status": str(team_row["status"] or ""),
            "account_role": str(team_row["account_role"] or ""),
            "device_code_auth_enabled": bool(team_row["device_code_auth_enabled"]),
            "error_count": int(team_row["error_count"] or 0),
            "last_sync": str(team_row["last_sync"] or ""),
            "created_at": str(team_row["created_at"] or ""),
            "client_id": str(team_row["client_id"] or ""),
            "team_accounts": team_accounts,
            "primary_account": primary_account or {},
        }

    def get_team_db_briefs(self, team_ids: list[int] | tuple[int, ...]) -> dict[int, dict[str, Any]]:
        briefs: dict[int, dict[str, Any]] = {}
        for team_id in self._normalize_team_ids(team_ids):
            detail = self._get_team_db_detail(team_id)
            if not detail:
                continue
            briefs[int(team_id)] = {
                "id": int(detail.get("id") or 0),
                "email": str(detail.get("email") or ""),
                "account_id": str(detail.get("account_id") or ""),
                "team_name": str(detail.get("team_name") or ""),
                "status": str(detail.get("status") or ""),
                "primary_account": dict(detail.get("primary_account") or {}),
            }
        return briefs

    def _has_live_sync_config(self) -> bool:
        return team_embedded_backend.is_available()

    def _invalidate_live_member_cache(self, team_id: int | None = None) -> None:
        with self._live_member_cache_lock:
            if team_id is None:
                self._live_member_cache.clear()
                return
            self._live_member_cache.pop(int(team_id), None)

    def _normalize_team_ids(self, team_ids: list[int] | tuple[int, ...]) -> list[int]:
        normalized: list[int] = []
        seen: set[int] = set()
        for raw_team_id in team_ids:
            team_id = int(raw_team_id)
            if team_id <= 0 or team_id in seen:
                continue
            normalized.append(team_id)
            seen.add(team_id)
        return normalized

    def _count_member_states(self, members: list[dict[str, Any]]) -> dict[str, int]:
        joined_count = 0
        invited_count = 0
        for item in members:
            status = str(item.get("status") or "").strip().lower()
            if status == "joined":
                joined_count += 1
            elif status == "invited":
                invited_count += 1
        return {
            "joined_count": joined_count,
            "invited_count": invited_count,
        }

    def _get_cached_live_member_counts(self, team_id: int, *, allow_stale: bool = False) -> dict[str, Any] | None:
        now = time.time()
        with self._live_member_cache_lock:
            entry = self._live_member_cache.get(int(team_id))
            if not entry:
                return None
            expires_at = float(entry.get("expires_at") or 0)
            if expires_at <= now and not allow_stale:
                return None
            payload = dict(entry.get("payload") or {})

        payload["live_sync_state"] = "cached" if expires_at > now else "stale-cache"
        payload.setdefault("live_sync_error", "")
        return payload

    def _store_live_member_counts(self, team_id: int, payload: dict[str, Any]) -> None:
        with self._live_member_cache_lock:
            self._live_member_cache[int(team_id)] = {
                "expires_at": time.time() + self.LIVE_MEMBER_CACHE_TTL_SECONDS,
                "payload": {
                    "joined_count": int(payload.get("joined_count") or 0),
                    "invited_count": int(payload.get("invited_count") or 0),
                },
            }

    def _get_live_member_counts(self, team_id: int) -> dict[str, Any]:
        if not self._has_live_sync_config():
            return {
                "live_sync_state": "disabled",
                "live_sync_error": "embedded team runtime 不可用",
            }

        cached = self._get_cached_live_member_counts(team_id)
        if cached is not None:
            return cached

        try:
            result = self.get_team_members(team_id)
            members = list(result.get("members") or [])
            payload = self._count_member_states(members)
            payload["live_sync_state"] = "live"
            payload["live_sync_error"] = ""
            self._store_live_member_counts(team_id, payload)
            return payload
        except Exception as exc:
            stale = self._get_cached_live_member_counts(team_id, allow_stale=True)
            if stale is not None:
                stale["live_sync_error"] = str(exc)
                return stale
            return {
                "live_sync_state": "fallback",
                "live_sync_error": str(exc),
            }

    def _apply_live_member_counts(self, item: dict[str, Any], live_counts: dict[str, Any]) -> None:
        state = str(live_counts.get("live_sync_state") or "fallback")
        item["live_sync_state"] = state
        item["live_sync_error"] = str(live_counts.get("live_sync_error") or "")
        item["live_members_synced"] = state in {"live", "cached", "stale-cache"}

        if not item["live_members_synced"]:
            return

        joined_count = int(live_counts.get("joined_count") or 0)
        invited_count = int(live_counts.get("invited_count") or 0)
        max_members = int(item.get("max_members") or 0)
        item["current_members"] = joined_count
        item["invited_members"] = invited_count
        item["remaining_slots"] = max(0, max_members - joined_count)

    def list_teams(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        search: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        page = max(int(page or 1), 1)
        page_size = max(1, min(int(page_size or 20), 200))
        search_text = str(search or "").strip().lower()
        status_text = str(status or "").strip().lower()

        where_parts: list[str] = []
        params: list[Any] = []

        if search_text:
            where_parts.append(
                "(" + " OR ".join(
                    [
                        "lower(email) LIKE ?",
                        "lower(ifnull(account_id, '')) LIKE ?",
                        "lower(ifnull(team_name, '')) LIKE ?",
                        "CAST(id AS TEXT) LIKE ?",
                    ]
                ) + ")"
            )
            like = f"%{search_text}%"
            params.extend([like, like, like, like])

        if status_text:
            where_parts.append("lower(ifnull(status, '')) = ?")
            params.append(status_text)

        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        offset = (page - 1) * page_size

        with self._connect_db() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS total FROM teams {where_clause}", params
            ).fetchone()
            total = int(total_row["total"] or 0) if total_row else 0

            rows = conn.execute(
                f"""
                SELECT
                  id,
                  email,
                  account_id,
                  team_name,
                  subscription_plan,
                  expires_at,
                  current_members,
                  max_members,
                  status,
                  last_sync,
                  created_at
                FROM teams
                {where_clause}
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()

        items: list[dict[str, Any]] = []
        live_sync_enabled = self._has_live_sync_config()
        for row in rows:
            current_members = int(row["current_members"] or 0)
            max_members = int(row["max_members"] or 0)
            item = {
                "id": int(row["id"] or 0),
                "email": str(row["email"] or ""),
                "account_id": str(row["account_id"] or ""),
                "team_name": str(row["team_name"] or ""),
                "subscription_plan": str(row["subscription_plan"] or ""),
                "expires_at": str(row["expires_at"] or ""),
                "current_members": current_members,
                "db_current_members": current_members,
                "max_members": max_members,
                "remaining_slots": max(0, max_members - current_members),
                "invited_members": 0,
                "status": str(row["status"] or ""),
                "last_sync": str(row["last_sync"] or ""),
                "created_at": str(row["created_at"] or ""),
                "live_members_synced": False,
                "live_sync_state": "pending" if live_sync_enabled else "disabled",
                "live_sync_error": "",
            }

            if live_sync_enabled:
                cached_counts = self._get_cached_live_member_counts(int(item["id"]), allow_stale=True)
                if cached_counts is not None:
                    self._apply_live_member_counts(item, cached_counts)
            else:
                item["live_sync_error"] = "embedded team runtime 未就绪"

            items.append(item)

        source_map = self._build_team_source_map(items)
        for item in items:
            item["source_account"] = dict(source_map.get(int(item.get("id") or 0)) or {})

        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    def sync_live_member_counts(self, team_ids: list[int]) -> dict[str, Any]:
        ids = self._normalize_team_ids(team_ids)
        if not ids:
            return {"items": []}

        max_workers = min(self.LIVE_MEMBER_FETCH_WORKERS, len(ids))
        items: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_workers or 1) as executor:
            future_map = {
                executor.submit(self._get_live_member_counts, team_id): team_id
                for team_id in ids
            }
            for future in as_completed(future_map):
                team_id = future_map[future]
                try:
                    payload = future.result()
                except Exception as exc:
                    payload = {
                        "live_sync_state": "fallback",
                        "live_sync_error": str(exc),
                    }
                items.append({
                    "id": team_id,
                    **payload,
                })

        return {"items": items}

    def import_teams(self, payload: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        allowed_keys = {
            "import_type",
            "access_token",
            "refresh_token",
            "session_token",
            "client_id",
            "email",
            "account_id",
            "content",
        }
        for key in allowed_keys:
            value = payload.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                value = value.strip()
            body[key] = value

        import_type = str(body.get("import_type") or "batch").strip().lower()
        body["import_type"] = import_type
        if import_type == "batch" and not str(body.get("content") or "").strip():
            raise RuntimeError("批量导入内容不能为空")
        if import_type == "single":
            if not any(str(body.get(key) or "").strip() for key in ("access_token", "refresh_token", "session_token")):
                raise RuntimeError("单个导入至少需要 access_token / refresh_token / session_token 其中之一")
            result = team_embedded_backend.import_team_single(body)
            self._invalidate_live_member_cache()
            return result

        events = team_embedded_backend.import_team_batch(str(body.get("content") or ""))
        self._invalidate_live_member_cache()

        finish = next((item for item in reversed(events) if isinstance(item, dict) and item.get("type") == "finish"), None)
        if finish:
            success_count = int(finish.get("success_count") or 0)
            failed_count = int(finish.get("failed_count") or 0)
            total = int(finish.get("total") or success_count + failed_count)
            return {
                "success": failed_count == 0,
                "message": f"批量导入完成：成功 {success_count}，失败 {failed_count}，总计 {total}",
                "total": total,
                "success_count": success_count,
                "failed_count": failed_count,
                "events": events,
            }

        if events:
            return {
                "success": True,
                "message": "批量导入请求已提交",
                "events": events,
            }

        return {"success": True, "message": "批量导入完成"}

    def get_team_info(self, team_id: int) -> dict[str, Any]:
        external = team_embedded_backend.get_team_info(team_id)
        detail = dict(external.get("team") or {})

        db_detail = self._get_team_db_detail(team_id)
        if db_detail:
            detail = {
                **db_detail,
                **detail,
            }

        live_counts = self._get_live_member_counts(team_id)
        detail.setdefault("db_current_members", int(detail.get("current_members") or 0))
        detail.setdefault("invited_members", 0)
        self._apply_live_member_counts(detail, live_counts)
        detail["source_account"] = dict(
            self._build_team_source_map([
                {
                    "id": int(detail.get("id") or team_id),
                    "email": str(detail.get("email") or ""),
                    "account_id": str(detail.get("account_id") or ""),
                }
            ]).get(int(detail.get("id") or team_id))
            or {}
        )

        return {
            **external,
            "team": detail,
        }

    def update_team(self, team_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        allowed_keys = {
            "email",
            "account_id",
            "access_token",
            "refresh_token",
            "session_token",
            "client_id",
            "max_members",
            "team_name",
            "status",
        }
        for key in allowed_keys:
            value = payload.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                value = value.strip()
            body[key] = value
        result = team_embedded_backend.update_team(int(team_id), body)
        self._invalidate_live_member_cache(team_id)
        return result

    def import_team_from_account(self, account_row_id: int) -> dict[str, Any]:
        account = self._get_account_for_team_import(int(account_row_id))
        if not account:
            raise RuntimeError(f"账号不存在: {account_row_id}")

        payload = self._build_team_import_payload_from_account(account)
        existing_team = self._find_existing_team(
            account_id=str(payload.get("account_id") or ""),
            email=str(payload.get("email") or ""),
        )
        source_account = {
            "account_db_id": int(account.id or 0),
            "email": str(account.email or ""),
            "account_id": str(account.user_id or ""),
            "workspace_scope": str(payload.get("workspace_scope") or ""),
            "workspace_label": str(payload.get("workspace_label") or ""),
            "status": str(account.status or ""),
            "has_refresh_token": bool(str(payload.get("refresh_token") or "").strip()),
        }

        if existing_team:
            result = self.update_team(
                int(existing_team["id"]),
                {
                    "email": payload.get("email"),
                    "account_id": payload.get("account_id"),
                    "access_token": payload.get("access_token"),
                    "refresh_token": payload.get("refresh_token"),
                    "session_token": payload.get("session_token"),
                    "client_id": payload.get("client_id"),
                },
            )
            if not result.get("success"):
                return result
            try:
                self.refresh_team(int(existing_team["id"]), force=True)
            except Exception:
                pass
            return {
                "success": True,
                "action": "updated",
                "team_id": int(existing_team["id"]),
                "message": f"Team 已存在，已更新凭证（Team #{int(existing_team['id'])}）",
                "source_account": source_account,
            }

        result = self.import_teams(
            {
                "import_type": "single",
                "email": payload.get("email"),
                "account_id": payload.get("account_id"),
                "access_token": payload.get("access_token"),
                "refresh_token": payload.get("refresh_token"),
                "session_token": payload.get("session_token"),
                "client_id": payload.get("client_id"),
            }
        )
        if result.get("success") and result.get("team_id"):
            try:
                self.refresh_team(int(result["team_id"]), force=True)
            except Exception:
                pass
        result["action"] = "created" if result.get("success") else "failed"
        result["source_account"] = source_account
        return result

    def refresh_team(self, team_id: int, *, force: bool = True) -> dict[str, Any]:
        result = team_embedded_backend.sync_team_info(int(team_id), force_refresh=bool(force))
        self._invalidate_live_member_cache(team_id)
        return result

    def batch_refresh_teams(self, team_ids: list[int]) -> dict[str, Any]:
        ids = self._normalize_team_ids(team_ids)
        if not ids:
            raise RuntimeError("请选择至少一个 Team")
        result = team_embedded_backend.batch_refresh_teams(ids)
        for team_id in ids:
            self._invalidate_live_member_cache(team_id)
        return result

    def delete_team(self, team_id: int) -> dict[str, Any]:
        result = team_embedded_backend.delete_team(int(team_id))
        self._invalidate_live_member_cache(team_id)
        return result

    def batch_delete_teams(self, team_ids: list[int]) -> dict[str, Any]:
        ids = self._normalize_team_ids(team_ids)
        if not ids:
            raise RuntimeError("请选择至少一个 Team")
        result = team_embedded_backend.batch_delete_teams(ids)
        for team_id in ids:
            self._invalidate_live_member_cache(team_id)
        return result

    def get_team_members(self, team_id: int) -> dict[str, Any]:
        return team_embedded_backend.get_team_members(int(team_id))

    def invite_member(self, team_id: int, email: str) -> dict[str, Any]:
        normalized_email = str(email or "").strip().lower()
        if not normalized_email:
            raise RuntimeError("邮箱不能为空")
        result = team_embedded_backend.add_team_member(int(team_id), normalized_email, verify_sync=False)
        self._invalidate_live_member_cache(team_id)
        return result

    def revoke_invite(self, team_id: int, email: str) -> dict[str, Any]:
        normalized_email = str(email or "").strip().lower()
        if not normalized_email:
            raise RuntimeError("邮箱不能为空")
        result = team_embedded_backend.revoke_team_invite(int(team_id), normalized_email)
        self._invalidate_live_member_cache(team_id)
        return result

    def delete_member(self, team_id: int, user_id: str) -> dict[str, Any]:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise RuntimeError("user_id 不能为空")
        result = team_embedded_backend.delete_team_member(int(team_id), normalized_user_id)
        self._invalidate_live_member_cache(team_id)
        return result

    def check_member(self, team_id: int, email: str, *, force: bool = False) -> dict[str, Any]:
        normalized_email = str(email or "").strip().lower()
        if not normalized_email:
            raise RuntimeError("邮箱不能为空")
        result = team_embedded_backend.check_member_status(int(team_id), normalized_email, force=bool(force))
        self._invalidate_live_member_cache(team_id)
        return result


team_lite_service = TeamLiteService()
