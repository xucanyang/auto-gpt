"""Persistent ChatGPT browser leases for operator-controlled Web Sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Callable
import uuid

from core.task_runtime import TaskInterruption


ACTIVE_LEASE_STATUSES = frozenset(
    {
        "reserved",
        "waiting_capacity",
        "authenticating",
        "refreshing_session",
        "ready_holding",
        "releasing",
    }
)
TERMINAL_LEASE_STATUSES = frozenset({"released", "stopped", "failed", "interrupted"})
SESSION_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "__Secure-authjs.session-token",
    "next-auth.session-token",
    "authjs.session-token",
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(error: Any) -> str:
    text = str(error or "").strip().replace("\r", " ").replace("\n", " ")
    return text[:500]


def _cookie_header_items(cookie_header: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in str(cookie_header or "").split(";"):
        if "=" not in raw:
            continue
        name, value = raw.split("=", 1)
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        items.append((name, value.strip()))
    return items


class WebSessionLeaseConflict(RuntimeError):
    def __init__(self, snapshot: dict[str, Any]):
        self.snapshot = dict(snapshot or {})
        super().__init__(
            "账号已有执行中的登录态任务"
            f"（task_id={self.snapshot.get('task_id') or '-'}，"
            f"状态={self.snapshot.get('status') or '-'}）"
        )


class WebSessionLeaseNotFound(KeyError):
    pass


class WebSessionLeaseReleaseRequested(TaskInterruption):
    """Cooperative browser shutdown requested by an operator."""


@dataclass
class _LeaseCommand:
    kind: str
    done: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class WebSessionLease:
    """One browser lifecycle owned by the thread that launched Playwright."""

    def __init__(
        self,
        *,
        manager: "WebSessionLeaseManager",
        lease_id: str,
        task_id: str,
        account_id: int,
        email: str,
        profile_dir: Path,
        cookie_header: str = "",
        session_token: str = "",
        device_id: str = "",
        on_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.manager = manager
        self.lease_id = lease_id
        self.task_id = str(task_id or "")
        self.account_id = int(account_id)
        self.email = str(email or "").strip()
        self.profile_dir = profile_dir
        self.storage_state_path = profile_dir / "storage-state.json"
        self.cookie_header = str(cookie_header or "").strip()
        self.session_token = str(session_token or "").strip()
        self.device_id = str(device_id or "").strip()
        self.status = "reserved"
        self.created_at = _utcnow_iso()
        self.updated_at = self.created_at
        self.ready_at = ""
        self.released_at = ""
        self.last_heartbeat_at = ""
        self.last_error = ""
        self.release_requested = False
        self.profile_saved = self.storage_state_path.is_file()
        self.restored_profile = self.profile_saved or bool(self.cookie_header or self.session_token)
        self.refresh_count = 0
        self._lock = threading.RLock()
        self._release_event = threading.Event()
        self._commands: queue.Queue[_LeaseCommand] = queue.Queue()
        self._current_command: _LeaseCommand | None = None
        self._on_change = on_change

    def set_on_change(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        with self._lock:
            self._on_change = callback

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            ready_timestamp = self.ready_at
            held_seconds = 0
            if ready_timestamp:
                try:
                    ready_epoch = datetime.fromisoformat(ready_timestamp).timestamp()
                    ended_epoch = time.time()
                    if self.released_at:
                        ended_epoch = datetime.fromisoformat(self.released_at).timestamp()
                    held_seconds = max(0, int(ended_epoch - ready_epoch))
                except (TypeError, ValueError, OverflowError):
                    held_seconds = 0
            return {
                "lease_id": self.lease_id,
                "task_id": self.task_id,
                "account_id": self.account_id,
                "email": self.email,
                "status": self.status,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "ready_at": self.ready_at,
                "released_at": self.released_at,
                "last_heartbeat_at": self.last_heartbeat_at,
                "held_seconds": held_seconds,
                "release_requested": self.release_requested,
                "profile_saved": self.profile_saved,
                "restored_profile": self.restored_profile,
                "profile_path": str(self.storage_state_path),
                "refresh_count": self.refresh_count,
                "error": self.last_error,
            }

    def _notify_change(self) -> None:
        snapshot = self.snapshot()
        self.manager._lease_changed(self, snapshot)
        callback = None
        with self._lock:
            callback = self._on_change
        if callable(callback):
            try:
                callback(snapshot)
            except Exception:
                pass

    def transition(self, status: str, *, error: Any = "") -> dict[str, Any]:
        normalized = str(status or "").strip().lower()
        if normalized not in ACTIVE_LEASE_STATUSES | TERMINAL_LEASE_STATUSES:
            raise ValueError(f"unsupported Web Session lease status: {normalized}")
        now = _utcnow_iso()
        with self._lock:
            if self.status in TERMINAL_LEASE_STATUSES and normalized != self.status:
                return self.snapshot()
            if (
                self.release_requested
                and self.status == "releasing"
                and normalized not in {"releasing", "released", "stopped", "failed", "interrupted"}
            ):
                return self.snapshot()
            self.status = normalized
            self.updated_at = now
            if normalized == "ready_holding":
                self.ready_at = self.ready_at or now
                self.last_heartbeat_at = now
                self.last_error = ""
            elif normalized in TERMINAL_LEASE_STATUSES:
                self.released_at = now
            if error:
                self.last_error = _safe_error(error)
        self._notify_change()
        return self.snapshot()

    def touch(self) -> None:
        now = _utcnow_iso()
        with self._lock:
            self.updated_at = now
            self.last_heartbeat_at = now

    def request_release(self) -> dict[str, Any]:
        with self._lock:
            if self.status in TERMINAL_LEASE_STATUSES:
                return self.snapshot()
            self.release_requested = True
            self.updated_at = _utcnow_iso()
            self._release_event.set()
            if self._current_command is not None:
                self._current_command.cancelled.set()
        self._fail_pending_commands("浏览器正在释放")
        return self.transition("releasing")

    def check_release_requested(self) -> None:
        if self._release_event.is_set():
            raise WebSessionLeaseReleaseRequested("已人工请求保存并释放浏览器")

    def request_refresh(self, *, timeout_seconds: float = 45.0) -> dict[str, Any]:
        with self._lock:
            if self.status != "ready_holding" or self.release_requested:
                raise RuntimeError("浏览器登录态当前不可刷新")
        command = _LeaseCommand(kind="refresh_session")
        self._commands.put(command)
        if not command.done.wait(timeout=max(float(timeout_seconds or 0), 1.0)):
            command.cancelled.set()
            raise TimeoutError("同步最新登录态超时")
        if command.error:
            raise RuntimeError(command.error)
        return dict(command.result or {})

    def browser_context_options(self) -> dict[str, Any]:
        if not self.storage_state_path.is_file():
            return {}
        try:
            payload = json.loads(self.storage_state_path.read_text("utf-8"))
        except (OSError, TypeError, ValueError):
            return {}
        if not isinstance(payload, dict) or not isinstance(payload.get("cookies"), list):
            return {}
        return {"storage_state": str(self.storage_state_path)}

    def seed_browser_context(self, context: Any) -> None:
        cookies: list[dict[str, Any]] = []
        names: set[str] = set()
        for name, value in _cookie_header_items(self.cookie_header):
            names.add(name)
            cookie: dict[str, Any] = {
                "name": name,
                "value": value,
                "secure": True,
                "httpOnly": "session-token" in name,
                "sameSite": "Lax",
            }
            if name.startswith("__Host-"):
                cookie["url"] = "https://chatgpt.com/"
            else:
                cookie["domain"] = ".chatgpt.com"
                cookie["path"] = "/"
            cookies.append(cookie)
        if self.session_token and not any(
            name == root or name.startswith(f"{root}.")
            for name in names
            for root in SESSION_COOKIE_NAMES
        ):
            cookies.append(
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": self.session_token,
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            )
        if self.device_id and "oai-did" not in names:
            cookies.append(
                {
                    "name": "oai-did",
                    "value": self.device_id,
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                }
            )
        if cookies:
            context.add_cookies(cookies)

    def checkpoint_profile(self, context: Any) -> bool:
        try:
            state = context.storage_state(indexed_db=True)
            if not isinstance(state, dict):
                raise RuntimeError("browser returned invalid storage state")
            self.profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(self.profile_dir, 0o700)
            except OSError:
                pass
            temporary = self.profile_dir / f".storage-state-{uuid.uuid4().hex}.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(state, handle, ensure_ascii=True, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.storage_state_path)
                try:
                    os.chmod(self.storage_state_path, 0o600)
                except OSError:
                    pass
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            with self._lock:
                self.profile_saved = True
                self.updated_at = _utcnow_iso()
            return True
        except Exception as exc:
            with self._lock:
                self.last_error = f"浏览器状态保存失败: {_safe_error(exc)}"
                self.updated_at = _utcnow_iso()
            return False

    def hold_browser(
        self,
        *,
        page: Any,
        context: Any,
        initial_payload: dict[str, Any],
        on_session_material: Callable[[dict[str, Any], str], dict[str, Any]],
        refresh_payload: Callable[[], dict[str, Any]],
        log: Callable[[str], None],
        stop_check: Callable[[], None] | None = None,
    ) -> None:
        self.checkpoint_profile(context)
        on_session_material(dict(initial_payload), "login")
        if self._release_event.is_set():
            self.transition("releasing")
            log("[执行登录态] 登录材料已保存；正在按人工请求关闭本地浏览器")
            return
        self.transition("ready_holding")
        log("[执行登录态] 登录态已就绪，浏览器将持续保持；等待人工停止并释放")

        next_page_check = time.monotonic() + 2.0
        try:
            while True:
                if self._release_event.wait(timeout=0.25):
                    self.transition("releasing")
                    self.checkpoint_profile(context)
                    log("[执行登录态] 已收到人工释放请求；状态已保存，正在关闭本地浏览器")
                    return
                if callable(stop_check):
                    stop_check()

                while True:
                    try:
                        command = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        if self._release_event.is_set():
                            command.cancelled.set()
                        if command.cancelled.is_set():
                            raise TimeoutError("同步请求已取消")
                        if command.kind != "refresh_session":
                            raise RuntimeError("不支持的浏览器登录态命令")
                        with self._lock:
                            self._current_command = command
                        self.transition("refreshing_session")
                        payload = refresh_payload()
                        if command.cancelled.is_set():
                            raise TimeoutError("同步请求已取消，结果未写回")
                        self.checkpoint_profile(context)
                        persisted = on_session_material(dict(payload), "refresh")
                        with self._lock:
                            self.refresh_count += 1
                        self.transition("ready_holding")
                        command.result = {
                            "ok": True,
                            "message": "已从保持中的浏览器同步最新登录态",
                            "lease": self.snapshot(),
                            "session": dict(persisted or {}),
                        }
                        log("[执行登录态] 已从保持中的浏览器同步最新 AT、Session 与 Cookie")
                    except TaskInterruption as exc:
                        command.error = _safe_error(exc) or "浏览器登录态命令已中断"
                        raise
                    except Exception as exc:
                        command.error = _safe_error(exc) or "同步最新登录态失败"
                        self.transition("ready_holding", error=command.error)
                    finally:
                        with self._lock:
                            if self._current_command is command:
                                self._current_command = None
                        command.done.set()

                now = time.monotonic()
                if now >= next_page_check:
                    next_page_check = now + 2.0
                    if bool(page.is_closed()):
                        raise RuntimeError("保持中的 ChatGPT 页面已关闭")
                    self.touch()
        except WebSessionLeaseReleaseRequested:
            self.checkpoint_profile(context)
            self.transition("releasing")
            self._fail_pending_commands("浏览器正在释放")
            log("[执行登录态] 已收到人工释放请求；状态已保存，正在关闭本地浏览器")
            return
        except TaskInterruption:
            self.checkpoint_profile(context)
            self.transition("stopped")
            self._fail_pending_commands("任务已停止")
            raise
        except Exception as exc:
            self.checkpoint_profile(context)
            self.transition("interrupted", error=exc)
            self._fail_pending_commands(_safe_error(exc) or "浏览器登录态已中断")
            raise

    def _fail_pending_commands(self, error: str) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            command.error = str(error or "浏览器登录态已结束")
            command.done.set()


class WebSessionLeaseManager:
    def __init__(self, *, runtime_dir: str | Path | None = None, max_history: int = 500) -> None:
        base = Path(runtime_dir or os.getenv("APP_RUNTIME_DIR") or "/runtime")
        self.profile_root = base / "chatgpt_browser_profiles"
        self.max_history = max(int(max_history or 0), 20)
        self._lock = threading.RLock()
        self._leases: dict[str, WebSessionLease] = {}
        self._active_by_account: dict[int, str] = {}

    def create(
        self,
        *,
        task_id: str,
        account_id: int,
        email: str,
        cookie_header: str = "",
        session_token: str = "",
        device_id: str = "",
        on_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> WebSessionLease:
        account_key = int(account_id)
        with self._lock:
            active_id = self._active_by_account.get(account_key)
            active = self._leases.get(active_id or "")
            if active is not None and active.status in ACTIVE_LEASE_STATUSES:
                raise WebSessionLeaseConflict(active.snapshot())
            lease_id = f"wsl_{uuid.uuid4().hex}"
            profile_dir = self.profile_root / str(account_key)
            lease = WebSessionLease(
                manager=self,
                lease_id=lease_id,
                task_id=task_id,
                account_id=account_key,
                email=email,
                profile_dir=profile_dir,
                cookie_header=cookie_header,
                session_token=session_token,
                device_id=device_id,
                on_change=on_change,
            )
            self._leases[lease_id] = lease
            self._active_by_account[account_key] = lease_id
            self._trim_history_locked()
        lease._notify_change()
        return lease

    def _trim_history_locked(self) -> None:
        if len(self._leases) <= self.max_history:
            return
        terminal = [
            lease
            for lease in self._leases.values()
            if lease.status in TERMINAL_LEASE_STATUSES
        ]
        terminal.sort(key=lambda lease: lease.created_at)
        for lease in terminal[: max(0, len(self._leases) - self.max_history)]:
            self._leases.pop(lease.lease_id, None)

    def _lease_changed(self, lease: WebSessionLease, snapshot: dict[str, Any]) -> None:
        with self._lock:
            if snapshot.get("status") in TERMINAL_LEASE_STATUSES:
                if self._active_by_account.get(lease.account_id) == lease.lease_id:
                    self._active_by_account.pop(lease.account_id, None)
            elif snapshot.get("status") in ACTIVE_LEASE_STATUSES:
                self._active_by_account[lease.account_id] = lease.lease_id

    def active_for_account(self, account_id: int) -> dict[str, Any] | None:
        with self._lock:
            lease = self._leases.get(self._active_by_account.get(int(account_id)) or "")
        if lease is None or lease.status not in ACTIVE_LEASE_STATUSES:
            return None
        return lease.snapshot()

    def snapshots_for_task(self, task_id: str) -> list[dict[str, Any]]:
        task_key = str(task_id or "")
        with self._lock:
            matches = [lease for lease in self._leases.values() if lease.task_id == task_key]
        latest_by_account: dict[int, WebSessionLease] = {}
        for lease in matches:
            current = latest_by_account.get(lease.account_id)
            if current is None or lease.created_at >= current.created_at:
                latest_by_account[lease.account_id] = lease
        return [
            latest_by_account[account_id].snapshot()
            for account_id in sorted(latest_by_account)
        ]

    def _active_task_leases(self, task_id: str, account_id: int | None = None) -> list[WebSessionLease]:
        task_key = str(task_id or "")
        with self._lock:
            return [
                lease
                for lease in self._leases.values()
                if lease.task_id == task_key
                and lease.status in ACTIVE_LEASE_STATUSES
                and (account_id is None or lease.account_id == int(account_id))
            ]

    def request_release(self, task_id: str, *, account_id: int | None = None) -> list[dict[str, Any]]:
        leases = self._active_task_leases(task_id, account_id)
        if not leases:
            snapshots = self.snapshots_for_task(task_id)
            if account_id is not None:
                snapshots = [item for item in snapshots if int(item.get("account_id") or 0) == int(account_id)]
            if snapshots:
                return snapshots
            raise WebSessionLeaseNotFound("浏览器登录态租约不存在")
        return [lease.request_release() for lease in leases]

    def request_refresh(
        self,
        task_id: str,
        *,
        account_id: int,
        timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        leases = self._active_task_leases(task_id, int(account_id))
        if not leases:
            raise WebSessionLeaseNotFound("保持中的浏览器登录态不存在")
        return leases[-1].request_refresh(timeout_seconds=timeout_seconds)


web_session_lease_manager = WebSessionLeaseManager()


__all__ = [
    "ACTIVE_LEASE_STATUSES",
    "TERMINAL_LEASE_STATUSES",
    "WebSessionLease",
    "WebSessionLeaseConflict",
    "WebSessionLeaseManager",
    "WebSessionLeaseNotFound",
    "WebSessionLeaseReleaseRequested",
    "web_session_lease_manager",
]
