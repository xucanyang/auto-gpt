from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable


DEFAULT_TEAM_MANAGER_CODE_PATHS = [
    "/embedded-team-manager",
    "/root/.openclaw/workspace/team-manage",
]
DEFAULT_TEAM_MANAGER_DB_PATHS = [
    "/runtime/team_manage.db",
    "/team-manage-seed-data/team_manage.db",
    "/root/.openclaw/workspace/team-manage/data/team_manage.db",
]
DEFAULT_TEAM_MANAGER_SECRET_KEY = "your-secret-key-here-change-in-production"


class TeamEmbeddedBackend:
    def __init__(self) -> None:
        self._loaded = False
        self._lock = threading.RLock()
        self._async_session_factory = None
        self._team_service = None
        self._chatgpt_service = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

    def _ensure_loaded(self) -> None:
        with self._lock:
            if self._loaded:
                return

            code_path = str(os.environ.get("TEAM_MANAGER_CODE_PATH") or "").strip()
            if not code_path:
                code_path = next((candidate for candidate in DEFAULT_TEAM_MANAGER_CODE_PATHS if Path(candidate).exists()), "")
            db_path = str(os.environ.get("TEAM_MANAGER_DB_PATH") or "").strip()
            if not db_path:
                db_path = next((candidate for candidate in DEFAULT_TEAM_MANAGER_DB_PATHS if Path(candidate).exists()), "")
            secret_key = str(os.environ.get("TEAM_MANAGER_SECRET_KEY") or DEFAULT_TEAM_MANAGER_SECRET_KEY).strip()

            if not code_path or not Path(code_path).exists():
                raise RuntimeError(f"embedded team-manager 代码目录不存在: {code_path or DEFAULT_TEAM_MANAGER_CODE_PATHS}")
            if not db_path or not Path(db_path).exists():
                raise RuntimeError(f"embedded team-manager 数据库不存在: {db_path or DEFAULT_TEAM_MANAGER_DB_PATHS}")

            os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
            os.environ.setdefault("SECRET_KEY", secret_key)
            os.environ.setdefault("APP_PORT", "8008")
            os.environ.setdefault("APP_HOST", "0.0.0.0")
            os.environ.setdefault("DEBUG", "false")
            os.environ.setdefault("JWT_VERIFY_SIGNATURE", "false")
            os.environ.setdefault("PROXY_ENABLED", "false")
            os.environ.setdefault("PROXY", "")
            os.environ.setdefault("TIMEZONE", "Asia/Shanghai")

            if code_path not in sys.path:
                sys.path.insert(0, code_path)

            from app.database import AsyncSessionLocal  # type: ignore
            from app.services.chatgpt import chatgpt_service  # type: ignore
            from app.services.team import team_service  # type: ignore

            self._async_session_factory = AsyncSessionLocal
            self._team_service = team_service
            self._chatgpt_service = chatgpt_service
            # curl_cffi AsyncSession 会绑定事件循环；嵌入模式改成固定 loop 后先清掉旧缓存。
            if hasattr(chatgpt_service, "_sessions"):
                chatgpt_service._sessions = {}
            self._loaded = True

    def _ensure_runtime_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop and self._loop.is_running():
                return self._loop

            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _worker() -> None:
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()

            thread = threading.Thread(target=_worker, name="team-embedded-loop", daemon=True)
            thread.start()
            ready.wait()
            self._loop = loop
            self._loop_thread = thread
            return loop

    def is_available(self) -> bool:
        try:
            self._ensure_loaded()
            self._ensure_runtime_loop()
            return True
        except Exception:
            return False

    def _run(self, callback: Callable[[Any, Any], Any]) -> Any:
        self._ensure_loaded()
        loop = self._ensure_runtime_loop()
        session_factory = self._async_session_factory
        team_service = self._team_service

        async def _runner() -> Any:
            async with session_factory() as session:
                return await callback(team_service, session)

        future = asyncio.run_coroutine_threadsafe(_runner(), loop)
        return future.result()

    def get_available_teams(self) -> dict[str, Any]:
        return self._run(lambda team_service, session: team_service.get_available_teams(session))

    def import_team_single(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda team_service, session: team_service.import_team_single(
                access_token=payload.get("access_token") or payload.get("token"),
                db_session=session,
                email=payload.get("email"),
                account_id=payload.get("account_id"),
                refresh_token=payload.get("refresh_token"),
                session_token=payload.get("session_token"),
                client_id=payload.get("client_id"),
            )
        )

    def import_team_batch(self, text: str) -> list[dict[str, Any]]:
        async def _collect(team_service, session):
            events: list[dict[str, Any]] = []
            async for item in team_service.import_team_batch(text, session):
                events.append(dict(item or {}))
            return events

        return self._run(_collect)

    def get_team_info(self, team_id: int) -> dict[str, Any]:
        return self._run(lambda team_service, session: team_service.get_team_by_id(int(team_id), session))

    def update_team(self, team_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._run(
            lambda team_service, session: team_service.update_team(
                team_id=int(team_id),
                db_session=session,
                email=payload.get("email"),
                account_id=payload.get("account_id"),
                access_token=payload.get("access_token"),
                refresh_token=payload.get("refresh_token"),
                session_token=payload.get("session_token"),
                client_id=payload.get("client_id"),
                max_members=payload.get("max_members"),
                team_name=payload.get("team_name"),
                status=payload.get("status"),
            )
        )

    def sync_team_info(self, team_id: int, *, force_refresh: bool = False) -> dict[str, Any]:
        return self._run(
            lambda team_service, session: team_service.sync_team_info(
                int(team_id),
                session,
                force_refresh=bool(force_refresh),
            )
        )

    def batch_refresh_teams(self, team_ids: list[int]) -> dict[str, Any]:
        async def _run_batch(team_service, session):
            success_count = 0
            failed_count = 0
            for raw_team_id in team_ids:
                try:
                    result = await team_service.sync_team_info(int(raw_team_id), session, force_refresh=True)
                    if result.get("success"):
                        success_count += 1
                    else:
                        failed_count += 1
                except Exception:
                    failed_count += 1
            return {
                "success": True,
                "message": f"批量刷新完成: 成功 {success_count}, 失败 {failed_count}",
                "success_count": success_count,
                "failed_count": failed_count,
            }

        return self._run(_run_batch)

    def delete_team(self, team_id: int) -> dict[str, Any]:
        return self._run(lambda team_service, session: team_service.delete_team(int(team_id), session))

    def batch_delete_teams(self, team_ids: list[int]) -> dict[str, Any]:
        async def _run_batch(team_service, session):
            success_count = 0
            failed_count = 0
            for raw_team_id in team_ids:
                try:
                    result = await team_service.delete_team(int(raw_team_id), session)
                    if result.get("success"):
                        success_count += 1
                    else:
                        failed_count += 1
                except Exception:
                    failed_count += 1
            return {
                "success": True,
                "message": f"批量删除完成: 成功 {success_count}, 失败 {failed_count}",
                "success_count": success_count,
                "failed_count": failed_count,
            }

        return self._run(_run_batch)

    def get_team_members(self, team_id: int) -> dict[str, Any]:
        return self._run(lambda team_service, session: team_service.get_team_members(int(team_id), session))

    def add_team_member(self, team_id: int, email: str, *, verify_sync: bool = True) -> dict[str, Any]:
        return self._run(
            lambda team_service, session: team_service.add_team_member(
                int(team_id),
                str(email or "").strip().lower(),
                session,
                verify_sync=bool(verify_sync),
            )
        )

    def revoke_team_invite(self, team_id: int, email: str) -> dict[str, Any]:
        return self._run(
            lambda team_service, session: team_service.revoke_team_invite(
                int(team_id),
                str(email or "").strip().lower(),
                session,
            )
        )

    def delete_team_member(self, team_id: int, user_id: str) -> dict[str, Any]:
        return self._run(
            lambda team_service, session: team_service.delete_team_member(
                int(team_id),
                str(user_id or "").strip(),
                session,
            )
        )

    def check_member_status(self, team_id: int, email: str, *, force: bool = False) -> dict[str, Any]:
        normalized_email = str(email or "").strip().lower()

        async def _check(team_service, session):
            if force:
                await team_service.sync_team_info(int(team_id), session, force_refresh=True)
            result = await team_service.get_team_members(int(team_id), session)
            members = list(result.get("members") or [])
            matched_item = None
            for item in members:
                if str(item.get("email") or "").strip().lower() == normalized_email:
                    matched_item = item
                    break
            status = str((matched_item or {}).get("status") or "").strip().lower()
            joined = status == "joined"
            invited = status == "invited"
            return {
                "success": True,
                "matched": bool(matched_item),
                "joined": joined,
                "invited": invited,
                "status": status,
                "member": matched_item,
            }

        return self._run(_check)


team_embedded_backend = TeamEmbeddedBackend()
