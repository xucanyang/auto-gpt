"""Persistent ChatGPT browser leases for operator-controlled Web Sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit
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
ADYEN_GCASH_REDIRECT_HOST = "checkoutshopper-live.adyen.com"
ADYEN_GCASH_REDIRECT_PATH = "/checkoutshopper/checkoutPaymentRedirect"
GCASH_COMMAND_HISTORY_LIMIT = 64
GCASH_REMOTE_STATES = frozenset(
    {
        "not_requested",
        "submitting",
        "queued",
        "running",
        "succeeded",
        "failed",
        "interrupted",
    }
)
_PROFILE_HOST_SUFFIXES = ("chatgpt.com", "openai.com")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(error: Any) -> str:
    text = str(error or "").strip().replace("\r", " ").replace("\n", " ")
    return text[:500]


def _safe_gcash_error(error: Any) -> str:
    text = _safe_error(error)
    return re.sub(r"https?://[^\s'\"]+", "[payment URL]", text)[:500]


def _url_digest(url: str) -> str:
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()[:24]


def _optional_expiry(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("GCash 到期时间无效") from None
    if parsed <= 0:
        raise ValueError("GCash 到期时间无效")
    return parsed


def validate_adyen_gcash_redirect_url(value: Any) -> str:
    """Return a validated official Adyen GCash redirect URL."""

    url = str(value or "").strip()
    if not url or len(url) > 8192 or any(ord(char) < 32 for char in url):
        raise ValueError("GCash 链接无效")
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("GCash 链接无效") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.netloc.lower() != ADYEN_GCASH_REDIRECT_HOST
        or parsed.path != ADYEN_GCASH_REDIRECT_PATH
        or parsed.fragment
    ):
        raise ValueError("GCash 链接不是官方 Adyen 支付跳转地址")
    redirect_values = query.get("redirectData") or []
    if not any(str(item or "").strip() for item in redirect_values):
        raise ValueError("GCash 链接缺少 redirectData")
    return url


def _profile_host_allowed(value: Any) -> bool:
    host = str(value or "").strip().lower().lstrip(".").rstrip(".")
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _PROFILE_HOST_SUFFIXES)


def _filtered_profile_storage_state(value: dict[str, Any]) -> dict[str, Any]:
    """Exclude payment-provider state before persisting a reusable ChatGPT profile."""

    state = dict(value)
    cookies = state.get("cookies")
    if isinstance(cookies, list):
        filtered_cookies: list[dict[str, Any]] = []
        for raw_cookie in cookies:
            if not isinstance(raw_cookie, dict):
                continue
            domain = raw_cookie.get("domain")
            if not domain and raw_cookie.get("url"):
                try:
                    domain = urlsplit(str(raw_cookie.get("url") or "")).hostname
                except (TypeError, ValueError):
                    domain = ""
            if _profile_host_allowed(domain):
                filtered_cookies.append(dict(raw_cookie))
        state["cookies"] = filtered_cookies
    origins = state.get("origins")
    if isinstance(origins, list):
        filtered_origins: list[dict[str, Any]] = []
        for raw_origin in origins:
            if not isinstance(raw_origin, dict):
                continue
            try:
                host = urlsplit(str(raw_origin.get("origin") or "")).hostname
            except (TypeError, ValueError):
                host = ""
            if _profile_host_allowed(host):
                filtered_origins.append(dict(raw_origin))
        state["origins"] = filtered_origins
    return state


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
    payload: dict[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: f"wsc_{uuid.uuid4().hex}")
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
        self.gcash_state = "not_requested"
        self.gcash_error = ""
        self.gcash_remote_request_id = ""
        self.gcash_remote_job_id = ""
        self.gcash_link_expires_at: int | None = None
        self.gcash_qr_expires_at: int | None = None
        self.gcash_link_digest = ""
        self.gcash_tab_state = "not_requested"
        self.gcash_tab_opened_at = ""
        self.gcash_tab_updated_at = ""
        self.gcash_tab_last_error = ""
        self.gcash_tab_command_id = ""
        self._lock = threading.RLock()
        self._release_event = threading.Event()
        self._commands: queue.Queue[_LeaseCommand] = queue.Queue()
        self._current_command: _LeaseCommand | None = None
        self._gcash_commands: dict[str, _LeaseCommand] = {}
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
                "gcash_state": self.gcash_state,
                "gcash_error": self.gcash_error,
                "gcash_remote_request_id": self.gcash_remote_request_id,
                "gcash_remote_job_id": self.gcash_remote_job_id,
                "gcash_link_expires_at": self.gcash_link_expires_at,
                "gcash_qr_expires_at": self.gcash_qr_expires_at,
                "gcash_link_digest": self.gcash_link_digest,
                "gcash_tab_state": self.gcash_tab_state,
                "gcash_tab_opened_at": self.gcash_tab_opened_at,
                "gcash_tab_updated_at": self.gcash_tab_updated_at,
                "gcash_tab_last_error": self.gcash_tab_last_error,
                "gcash_tab_command_id": self.gcash_tab_command_id,
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

    def update_gcash_status(
        self,
        state: str,
        *,
        error: Any = "",
        remote_request_id: str = "",
        remote_job_id: str = "",
        link_expires_at: Any = None,
        gcash_qr_expires_at: Any = None,
    ) -> dict[str, Any]:
        normalized = str(state or "").strip().lower()
        if normalized not in GCASH_REMOTE_STATES:
            raise ValueError(f"unsupported GCash state: {normalized}")
        request_id = str(remote_request_id or "").strip()[:256]
        job_id = str(remote_job_id or "").strip()[:256]
        link_expiry = _optional_expiry(link_expires_at)
        qr_expiry = _optional_expiry(gcash_qr_expires_at)
        now = _utcnow_iso()
        with self._lock:
            if request_id:
                self.gcash_remote_request_id = request_id
            if job_id:
                self.gcash_remote_job_id = job_id
            if link_expires_at not in (None, ""):
                self.gcash_link_expires_at = link_expiry
            if gcash_qr_expires_at not in (None, ""):
                self.gcash_qr_expires_at = qr_expiry
            self.gcash_state = normalized
            self.gcash_error = _safe_gcash_error(error) if error else ""
            self.updated_at = now
        self._notify_change()
        return self.snapshot()

    def request_open_gcash(
        self,
        *,
        url: str,
        remote_request_id: str,
        remote_job_id: str = "",
        timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        request_id = str(remote_request_id or "").strip()
        if not request_id:
            raise ValueError("GCash remote_request_id 不能为空")
        if len(request_id) > 256:
            raise ValueError("GCash remote_request_id 过长")
        job_id = str(remote_job_id or "").strip()[:256]
        try:
            validated_url = validate_adyen_gcash_redirect_url(url)
        except ValueError as exc:
            now = _utcnow_iso()
            changed = False
            with self._lock:
                if self.status == "ready_holding" and not self.release_requested:
                    self.gcash_remote_request_id = request_id
                    if job_id:
                        self.gcash_remote_job_id = job_id
                    self.gcash_link_digest = _url_digest(str(url or ""))
                    self.gcash_tab_state = "failed"
                    self.gcash_tab_updated_at = now
                    self.gcash_tab_last_error = _safe_gcash_error(exc)
                    self.gcash_tab_command_id = ""
                    self.updated_at = now
                    changed = True
            if changed:
                self._notify_change()
            raise
        digest = _url_digest(validated_url)
        command: _LeaseCommand
        is_new = False
        with self._lock:
            if self.status != "ready_holding" or self.release_requested:
                raise RuntimeError("浏览器登录态当前不能打开 GCash 链接")
            existing = self._gcash_commands.get(request_id)
            if existing is not None:
                if str(existing.payload.get("url_digest") or "") != digest:
                    raise RuntimeError("同一 GCash remote_request_id 对应了不同链接")
                existing_job_id = str(existing.payload.get("remote_job_id") or "")
                if existing_job_id and job_id and existing_job_id != job_id:
                    raise RuntimeError("同一 GCash remote_request_id 对应了不同远端任务")
                command = existing
            else:
                if any(not item.done.is_set() for item in self._gcash_commands.values()):
                    raise RuntimeError("已有 GCash 标签页打开命令正在执行")
                command = _LeaseCommand(
                    kind="open_gcash_link",
                    payload={
                        "url": validated_url,
                        "url_digest": digest,
                        "remote_request_id": request_id,
                        "remote_job_id": job_id,
                        "navigation_timeout_ms": max(
                            1_000,
                            min(int(max(float(timeout_seconds or 0), 0.1) * 1000), 30_000),
                        ),
                    },
                )
                self._gcash_commands[request_id] = command
                self._trim_gcash_commands_locked()
                self.gcash_remote_request_id = request_id
                if job_id:
                    self.gcash_remote_job_id = job_id
                self.gcash_link_digest = digest
                self.gcash_tab_state = "opening"
                self.gcash_tab_updated_at = _utcnow_iso()
                self.gcash_tab_last_error = ""
                self.gcash_tab_command_id = command.command_id
                self.updated_at = self.gcash_tab_updated_at
                is_new = True
        if is_new:
            self._notify_change()
            self._commands.put(command)
        wait_seconds = max(float(timeout_seconds or 0), 0.05)
        if not command.done.wait(timeout=wait_seconds):
            command.cancelled.set()
            self._record_gcash_command_failure(command, "GCash 标签页打开超时", state="timed_out")
            raise TimeoutError("GCash 标签页打开超时")
        if command.error:
            raise RuntimeError(command.error)
        return dict(command.result or {})

    def _trim_gcash_commands_locked(self) -> None:
        while len(self._gcash_commands) > GCASH_COMMAND_HISTORY_LIMIT:
            removable = next(
                (
                    request_id
                    for request_id, command in self._gcash_commands.items()
                    if command.done.is_set()
                ),
                None,
            )
            if removable is None:
                return
            self._gcash_commands.pop(removable, None)

    def _record_gcash_command_failure(
        self,
        command: _LeaseCommand,
        error: Any,
        *,
        state: str = "failed",
    ) -> None:
        safe_error = _safe_gcash_error(error) or "GCash 标签页打开失败"
        request_id = str(command.payload.get("remote_request_id") or "")
        now = _utcnow_iso()
        with self._lock:
            if self.gcash_remote_request_id == request_id:
                self.gcash_tab_state = state
                self.gcash_tab_updated_at = now
                self.gcash_tab_last_error = safe_error
                self.gcash_tab_command_id = command.command_id
                self.updated_at = now
            command.error = safe_error
            command.payload.pop("url", None)
            command.done.set()
        self._notify_change()

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
            state = _filtered_profile_storage_state(state)
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

    def _process_refresh_command(
        self,
        command: _LeaseCommand,
        *,
        context: Any,
        on_session_material: Callable[[dict[str, Any], str], dict[str, Any]],
        refresh_payload: Callable[[], dict[str, Any]],
        log: Callable[[str], None],
    ) -> None:
        if command.cancelled.is_set():
            raise TimeoutError("同步请求已取消")
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

    def _process_open_gcash_command(
        self,
        command: _LeaseCommand,
        *,
        context: Any,
        gcash_page_holder: dict[str, Any],
        log: Callable[[str], None],
    ) -> None:
        if command.cancelled.is_set():
            raise TimeoutError(command.error or "GCash 标签页打开请求已取消")
        url = validate_adyen_gcash_redirect_url(command.payload.get("url"))
        request_id = str(command.payload.get("remote_request_id") or "")
        pages = gcash_page_holder.setdefault("pages", {})
        if not isinstance(pages, dict):
            pages = {}
            gcash_page_holder["pages"] = pages
        # A page belongs to one remote request for its entire lifetime.  This
        # prevents a retry for a fresh request from navigating an older QR tab.
        gcash_page = pages.get(request_id)
        reused = gcash_page is not None and not bool(gcash_page.is_closed())
        if not reused:
            gcash_page = context.new_page()
            pages[request_id] = gcash_page
        if command.cancelled.is_set():
            raise TimeoutError(command.error or "GCash 标签页打开请求已取消")
        gcash_page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=int(command.payload.get("navigation_timeout_ms") or 30_000),
        )
        if command.cancelled.is_set() or self._release_event.is_set():
            raise TimeoutError(command.error or "GCash 标签页打开请求已取消")
        if bool(gcash_page.is_closed()):
            raise RuntimeError("GCash 标签页导航后已关闭")
        current_url = str(getattr(gcash_page, "url", "") or "").strip()
        if not current_url or current_url == "about:blank":
            raise RuntimeError("GCash 标签页未完成导航")
        bring_to_front = getattr(gcash_page, "bring_to_front", None)
        if callable(bring_to_front):
            bring_to_front()
        now = _utcnow_iso()
        with self._lock:
            if self.gcash_remote_request_id == request_id:
                self.gcash_tab_state = "ready"
                self.gcash_tab_opened_at = now
                self.gcash_tab_updated_at = now
                self.gcash_tab_last_error = ""
                self.gcash_tab_command_id = command.command_id
                self.updated_at = now
            command.result = {
                "ok": True,
                "message": "已在对应账号的登录态浏览器中打开 GCash 标签页",
                "reused_tab": reused,
                "command_id": command.command_id,
                "remote_request_id": request_id,
                "lease": self.snapshot(),
            }
            command.payload.pop("url", None)
            command.done.set()
        self._notify_change()
        log(
            "[执行登录态] 已在当前账号浏览器中"
            f"{'复用' if reused else '新建'} GCash 标签页｜request_id={request_id}"
        )

    def _close_gcash_page(
        self,
        gcash_page_holder: dict[str, Any] | None,
        request_id: str,
    ) -> None:
        pages = (gcash_page_holder or {}).get("pages", {})
        if not isinstance(pages, dict):
            return
        page = pages.get(str(request_id or ""))
        try:
            if page is not None and not bool(page.is_closed()):
                close = getattr(page, "close", None)
                if callable(close):
                    close()
        except Exception:
            pass

    def _mark_gcash_tab_closed(
        self,
        gcash_page_holder: dict[str, Any] | None = None,
        *,
        close_pages: bool = True,
    ) -> None:
        now = _utcnow_iso()
        changed = False
        pages = (gcash_page_holder or {}).get("pages", {})
        if close_pages and isinstance(pages, dict):
            # Playwright objects must be closed by the owner thread.  The
            # surrounding browser cleanup remains responsible for the context
            # itself, while this explicit close makes release deterministic for
            # every generated GCash tab.
            for page in list(pages.values()):
                try:
                    if page is not None and not bool(page.is_closed()):
                        close = getattr(page, "close", None)
                        if callable(close):
                            close()
                except Exception:
                    pass
        with self._lock:
            if self.gcash_tab_state in {
                "opening",
                "ready",
                "timed_out",
                "failed",
                "cancelled",
            }:
                self.gcash_tab_state = "closed"
                self.gcash_tab_updated_at = now
                self.updated_at = now
                changed = True
        if changed:
            self._notify_change()

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
        gcash_page_holder: dict[str, Any] = {"pages": {}}
        try:
            while True:
                if self._release_event.wait(timeout=0.25):
                    self.transition("releasing")
                    self.checkpoint_profile(context)
                    self._mark_gcash_tab_closed(gcash_page_holder)
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
                            raise TimeoutError(command.error or "浏览器登录态命令已取消")
                        with self._lock:
                            self._current_command = command
                        if command.kind == "refresh_session":
                            self._process_refresh_command(
                                command,
                                context=context,
                                on_session_material=on_session_material,
                                refresh_payload=refresh_payload,
                                log=log,
                            )
                        elif command.kind == "open_gcash_link":
                            self._process_open_gcash_command(
                                command,
                                context=context,
                                gcash_page_holder=gcash_page_holder,
                                log=log,
                            )
                        else:
                            raise RuntimeError("不支持的浏览器登录态命令")
                    except TaskInterruption as exc:
                        command.error = _safe_error(exc) or "浏览器登录态命令已中断"
                        raise
                    except Exception as exc:
                        if command.kind == "open_gcash_link":
                            self._close_gcash_page(
                                gcash_page_holder,
                                str(command.payload.get("remote_request_id") or ""),
                            )
                            failure_state = (
                                "timed_out"
                                if "超时" in str(command.error or exc)
                                else "cancelled"
                                if command.cancelled.is_set() or self._release_event.is_set()
                                else "failed"
                            )
                            self._record_gcash_command_failure(
                                command,
                                command.error or exc,
                                state=failure_state,
                            )
                        else:
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
                    pages = gcash_page_holder.get("pages", {})
                    latest_page = (
                        pages.get(self.gcash_remote_request_id)
                        if isinstance(pages, dict) and self.gcash_remote_request_id
                        else None
                    )
                    if (
                        latest_page is not None
                        and self.gcash_tab_state == "ready"
                        and bool(latest_page.is_closed())
                    ):
                        self._mark_gcash_tab_closed(gcash_page_holder, close_pages=False)
                    self.touch()
        except WebSessionLeaseReleaseRequested:
            self.checkpoint_profile(context)
            self.transition("releasing")
            self._fail_pending_commands("浏览器正在释放")
            self._mark_gcash_tab_closed(gcash_page_holder)
            log("[执行登录态] 已收到人工释放请求；状态已保存，正在关闭本地浏览器")
            return
        except TaskInterruption:
            self.checkpoint_profile(context)
            self.transition("stopped")
            self._fail_pending_commands("任务已停止")
            self._mark_gcash_tab_closed(gcash_page_holder)
            raise
        except Exception as exc:
            self.checkpoint_profile(context)
            self.transition("interrupted", error=exc)
            self._fail_pending_commands(_safe_error(exc) or "浏览器登录态已中断")
            self._mark_gcash_tab_closed(gcash_page_holder)
            raise

    def _fail_pending_commands(self, error: str) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            command.cancelled.set()
            if command.kind == "open_gcash_link":
                self._record_gcash_command_failure(
                    command,
                    error or "浏览器登录态已结束",
                    state="cancelled",
                )
            else:
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

    def active_count(self) -> int:
        with self._lock:
            return sum(
                1 for lease in self._leases.values() if lease.status in ACTIVE_LEASE_STATUSES
            )

    def available_capacity(self, limit: int) -> int:
        try:
            normalized_limit = max(int(limit), 0)
        except (TypeError, ValueError, OverflowError):
            normalized_limit = 0
        with self._lock:
            active = sum(
                1 for lease in self._leases.values() if lease.status in ACTIVE_LEASE_STATUSES
            )
        return max(normalized_limit - active, 0)

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

    def _target_lease(
        self,
        task_id: str,
        *,
        account_id: int,
        lease_id: str,
    ) -> WebSessionLease:
        task_key = str(task_id or "")
        lease_key = str(lease_id or "").strip()
        account_key = int(account_id)
        with self._lock:
            lease = self._leases.get(lease_key)
            if (
                lease is None
                or lease.task_id != task_key
                or lease.account_id != account_key
            ):
                raise WebSessionLeaseNotFound("浏览器登录态租约不存在或身份不匹配")
            return lease

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

    def snapshot_for(
        self,
        task_id: str,
        *,
        account_id: int,
        lease_id: str,
    ) -> dict[str, Any]:
        """Return one lease snapshot after checking all ownership keys."""

        return self._target_lease(
            task_id,
            account_id=int(account_id),
            lease_id=lease_id,
        ).snapshot()

    def is_ready(
        self,
        task_id: str,
        *,
        account_id: int,
        lease_id: str,
    ) -> bool:
        """Check that a manual child task may still write to its owner lease."""

        try:
            lease = self._target_lease(
                task_id,
                account_id=int(account_id),
                lease_id=lease_id,
            )
        except (WebSessionLeaseNotFound, TypeError, ValueError):
            return False
        with lease._lock:
            return lease.status == "ready_holding" and not lease.release_requested

    def update_gcash_status(
        self,
        task_id: str,
        *,
        account_id: int,
        lease_id: str,
        state: str,
        error: Any = "",
        remote_request_id: str = "",
        remote_job_id: str = "",
        link_expires_at: Any = None,
        gcash_qr_expires_at: Any = None,
    ) -> dict[str, Any]:
        lease = self._target_lease(
            task_id,
            account_id=int(account_id),
            lease_id=lease_id,
        )
        return lease.update_gcash_status(
            state,
            error=error,
            remote_request_id=remote_request_id,
            remote_job_id=remote_job_id,
            link_expires_at=link_expires_at,
            gcash_qr_expires_at=gcash_qr_expires_at,
        )

    def request_open_gcash(
        self,
        task_id: str,
        *,
        account_id: int,
        lease_id: str,
        url: str,
        remote_request_id: str,
        remote_job_id: str = "",
        timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        lease = self._target_lease(
            task_id,
            account_id=int(account_id),
            lease_id=lease_id,
        )
        return lease.request_open_gcash(
            url=url,
            remote_request_id=remote_request_id,
            remote_job_id=remote_job_id,
            timeout_seconds=timeout_seconds,
        )

    def request_open_gcash_for_active_account(
        self,
        *,
        account_id: int,
        url: str,
        remote_request_id: str,
        remote_job_id: str = "",
        link_expires_at: Any = None,
        gcash_qr_expires_at: Any = None,
        timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        """Open a generated link in the account's currently held browser.

        The payment-link task and the browser-owning task may be different.  The
        account's one-active-lease invariant is the routing authority here; the
        selected lease still validates its own ready/release state before the
        owner thread receives the command.
        """

        account_key = int(account_id)
        with self._lock:
            lease = self._leases.get(self._active_by_account.get(account_key) or "")
        if lease is None or lease.status not in ACTIVE_LEASE_STATUSES:
            raise WebSessionLeaseNotFound("账号没有保持中的登录态浏览器")
        if lease.status != "ready_holding" or lease.release_requested:
            raise WebSessionLeaseNotFound("账号登录态浏览器尚未就绪或正在释放")
        lease.update_gcash_status(
            "succeeded",
            remote_request_id=remote_request_id,
            remote_job_id=remote_job_id,
            link_expires_at=link_expires_at,
            gcash_qr_expires_at=gcash_qr_expires_at,
        )
        return lease.request_open_gcash(
            url=url,
            remote_request_id=remote_request_id,
            remote_job_id=remote_job_id,
            timeout_seconds=timeout_seconds,
        )


web_session_lease_manager = WebSessionLeaseManager()


__all__ = [
    "ADYEN_GCASH_REDIRECT_HOST",
    "ADYEN_GCASH_REDIRECT_PATH",
    "ACTIVE_LEASE_STATUSES",
    "GCASH_REMOTE_STATES",
    "TERMINAL_LEASE_STATUSES",
    "WebSessionLease",
    "WebSessionLeaseConflict",
    "WebSessionLeaseManager",
    "WebSessionLeaseNotFound",
    "WebSessionLeaseReleaseRequested",
    "validate_adyen_gcash_redirect_url",
    "web_session_lease_manager",
]
