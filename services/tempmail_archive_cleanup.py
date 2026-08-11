"""TempMail mailbox archive and cleanup scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from core.timezone import beijing_from_timestamp


DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_KEEP_RECENT_MINUTES = 60
DEFAULT_THRESHOLD = 100
DEFAULT_MAILBOX = "b@cccy.me"
LOOP_INTERVAL_SECONDS = 60
MAX_LIST_PAGES = 50
PAGE_SIZE = 100

_state_lock = threading.Lock()
_run_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_running = False
_next_run_at = 0.0
_last_run_at = 0.0
_last_success_at = 0.0
_last_error = ""
_last_result: dict[str, Any] = {}


@dataclass
class TempMailArchiveCleanupConfig:
    enabled: bool
    interval_minutes: int
    keep_recent_minutes: int
    threshold: int
    pause_active_tasks: bool
    mailbox: str
    backup_path: str
    tempmail_api_url: str
    tempmail_api_key: str
    tempmail_api_key_header: str


def _config_store():
    from core.config_store import config_store

    return config_store


def _to_bool(value: str | None, default: bool = False) -> bool:
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _to_int(value: str | None, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(float(str(value or "").strip())))
    except Exception:
        return default


def _now() -> float:
    return time.time()


def _iso(timestamp: float) -> str:
    return beijing_from_timestamp(timestamp)


def default_backup_path() -> str:
    runtime_dir = str(os.getenv("APP_RUNTIME_DIR") or "").strip()
    if runtime_dir:
        return str(Path(runtime_dir) / "tempmail_email_backups.db")
    return "data/tempmail_email_backups.db"


def get_tempmail_archive_cleanup_config() -> TempMailArchiveCleanupConfig:
    store = _config_store()
    mailbox = (
        str(store.get("tempmail_archive_cleanup_mailbox", "") or "").strip()
        or str(store.get("icloud_forward_to", DEFAULT_MAILBOX) or DEFAULT_MAILBOX).strip()
        or DEFAULT_MAILBOX
    )
    return TempMailArchiveCleanupConfig(
        enabled=_to_bool(store.get("tempmail_archive_cleanup_enabled", ""), default=False),
        interval_minutes=_to_int(
            store.get("tempmail_archive_cleanup_interval_minutes", ""),
            DEFAULT_INTERVAL_MINUTES,
            minimum=1,
        ),
        keep_recent_minutes=_to_int(
            store.get("tempmail_archive_cleanup_keep_recent_minutes", ""),
            DEFAULT_KEEP_RECENT_MINUTES,
            minimum=1,
        ),
        threshold=_to_int(
            store.get("tempmail_archive_cleanup_threshold", ""),
            DEFAULT_THRESHOLD,
            minimum=1,
        ),
        pause_active_tasks=_to_bool(
            store.get("tempmail_archive_cleanup_pause_active_tasks", ""),
            default=True,
        ),
        mailbox=mailbox,
        backup_path=str(store.get("tempmail_archive_cleanup_backup_path", "") or "").strip()
        or default_backup_path(),
        tempmail_api_url=str(store.get("tempmail_api_url", "") or "").strip(),
        tempmail_api_key=str(store.get("tempmail_api_key", "") or "").strip(),
        tempmail_api_key_header=str(store.get("tempmail_api_key_header", "Authorization") or "Authorization").strip()
        or "Authorization",
    )


def _schedule_next(config: TempMailArchiveCleanupConfig, *, base_timestamp: float | None = None) -> float:
    global _next_run_at

    next_run_at = float(base_timestamp if base_timestamp is not None else _now()) + config.interval_minutes * 60
    with _state_lock:
        _next_run_at = next_run_at
    return next_run_at


def _ensure_initial_next_run(config: TempMailArchiveCleanupConfig) -> None:
    with _state_lock:
        current_next_run = _next_run_at
    if current_next_run > 0:
        return
    _schedule_next(config)


def _record_error(message: str) -> None:
    global _last_error

    with _state_lock:
        _last_error = str(message or "").strip()


def _active_task_snapshots() -> list[dict[str, Any]]:
    try:
        from api.tasks import _task_store

        snapshots = _task_store.list_snapshots()
    except Exception:
        return []
    active: list[dict[str, Any]] = []
    for item in snapshots:
        status = str(item.get("status") or "").strip().lower()
        if status in {"pending", "running"}:
            active.append(item)
    return active


def _has_active_tasks() -> bool:
    return bool(_active_task_snapshots())


def _resolve_backup_path(raw_path: str) -> Path:
    path = Path(str(raw_path or "").strip() or default_backup_path())
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return json.dumps(str(value), ensure_ascii=False)


def _open_archive_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tempmail_email_archive (
            mailbox_email TEXT NOT NULL,
            mailbox_id TEXT NOT NULL,
            email_id TEXT NOT NULL,
            subject TEXT,
            sender TEXT,
            received_at TEXT,
            received_at_ts REAL,
            received_for_json TEXT,
            body_text TEXT,
            body_html TEXT,
            raw_message TEXT,
            summary_json TEXT,
            detail_json TEXT,
            backup_at TEXT NOT NULL,
            deleted_at TEXT,
            delete_status TEXT,
            PRIMARY KEY (mailbox_id, email_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tempmail_email_archive_mailbox ON tempmail_email_archive(mailbox_email)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tempmail_email_archive_received_at ON tempmail_email_archive(received_at_ts)"
    )
    conn.commit()
    return conn


def _pick_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, (dict, list)):
            return _json_dumps(value)
        return str(value)
    return ""


def _message_id(message: dict[str, Any], index: int = 0) -> str:
    from core.base_mailbox import TempMailLocalMailbox

    return TempMailLocalMailbox._message_id(message, index)


def _parse_message_timestamp(message: dict[str, Any]) -> float | None:
    from core.base_mailbox import TempMailLocalMailbox

    return TempMailLocalMailbox._parse_message_timestamp(message)


def _merge_email_payload(summary: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    merged = dict(summary or {})
    merged.update(dict(detail or {}))
    return merged


def _archive_email(
    conn: sqlite3.Connection,
    *,
    mailbox_email: str,
    mailbox_id: str,
    email_id: str,
    summary: dict[str, Any],
    detail: dict[str, Any],
    backup_at: str,
) -> None:
    merged = _merge_email_payload(summary, detail)
    received_for = merged.get("received_for")
    if not isinstance(received_for, list):
        received_for = []
    received_at_ts = _parse_message_timestamp(merged)
    received_at = _pick_text(
        merged,
        ("received_at", "receivedAt", "created_at", "createdAt", "date", "timestamp"),
    )
    existing = conn.execute(
        "SELECT deleted_at, delete_status FROM tempmail_email_archive WHERE mailbox_id=? AND email_id=?",
        (mailbox_id, email_id),
    ).fetchone()
    deleted_at = existing[0] if existing else None
    delete_status = existing[1] if existing else None
    conn.execute(
        """
        INSERT OR REPLACE INTO tempmail_email_archive (
            mailbox_email,
            mailbox_id,
            email_id,
            subject,
            sender,
            received_at,
            received_at_ts,
            received_for_json,
            body_text,
            body_html,
            raw_message,
            summary_json,
            detail_json,
            backup_at,
            deleted_at,
            delete_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mailbox_email,
            mailbox_id,
            email_id,
            _pick_text(merged, ("subject", "title")),
            _pick_text(merged, ("from", "sender", "sender_email", "from_address")),
            received_at,
            received_at_ts,
            _json_dumps(received_for),
            _pick_text(merged, ("body_text", "text", "plain", "content", "body")),
            _pick_text(merged, ("body_html", "html", "html_body")),
            _pick_text(merged, ("raw_message", "raw", "source")),
            _json_dumps(summary),
            _json_dumps(detail),
            backup_at,
            deleted_at,
            delete_status,
        ),
    )


def _mark_deleted(conn: sqlite3.Connection, *, mailbox_id: str, email_id: str, status: str = "deleted") -> None:
    conn.execute(
        """
        UPDATE tempmail_email_archive
        SET deleted_at=?, delete_status=?
        WHERE mailbox_id=? AND email_id=?
        """,
        (datetime.now(timezone.utc).isoformat(), status, mailbox_id, email_id),
    )


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "emails", "items", "messages", "mails", "list", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_items(value)
            if nested:
                return nested
    return []


def _list_all_emails(client: Any, mailbox_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for page in range(1, MAX_LIST_PAGES + 1):
        response = client._request(
            "GET",
            f"/api/mailboxes/{mailbox_id}/emails",
            headers=client._headers(),
            params={"page": page, "size": PAGE_SIZE},
            timeout=15,
        )
        if response.status_code != 200:
            raise RuntimeError(f"TempMail Ready API 列邮件失败: {response.status_code} {response.text[:200]}")
        page_items = _extract_items(response.json())
        new_count = 0
        for item in page_items:
            email_id = _message_id(item, len(items))
            if email_id in seen_ids:
                continue
            seen_ids.add(email_id)
            items.append(item)
            new_count += 1
        if len(page_items) < PAGE_SIZE or new_count == 0:
            break
    return items


def _delete_email(client: Any, mailbox_id: str, email_id: str) -> tuple[bool, str]:
    response = client._request(
        "DELETE",
        f"/api/mailboxes/{mailbox_id}/emails/{email_id}",
        headers=client._headers(),
        timeout=15,
    )
    if response.status_code in {200, 202, 204}:
        return True, "deleted"
    if response.status_code == 404:
        return True, "remote_missing"
    return False, f"{response.status_code} {str(response.text or '')[:200]}"


def _make_tempmail_client(config: TempMailArchiveCleanupConfig):
    from core.base_mailbox import TempMailLocalMailbox

    return TempMailLocalMailbox(
        api_url=config.tempmail_api_url,
        api_key=config.tempmail_api_key,
        api_key_header=config.tempmail_api_key_header,
        ttl_minutes=60,
        permanent=True,
    )


def _build_base_result(config: TempMailArchiveCleanupConfig) -> dict[str, Any]:
    return {
        "mailbox": config.mailbox,
        "backup_path": config.backup_path,
        "threshold": config.threshold,
        "keep_recent_minutes": config.keep_recent_minutes,
        "interval_minutes": config.interval_minutes,
        "pause_active_tasks": config.pause_active_tasks,
    }


def run_once(
    *,
    force: bool = False,
    ignore_active_tasks: bool = False,
    delete: bool = True,
) -> dict[str, Any]:
    global _last_run_at, _last_success_at, _last_error, _last_result

    config = get_tempmail_archive_cleanup_config()
    active_tasks = _active_task_snapshots()
    base = _build_base_result(config)
    base["active_task_count"] = len(active_tasks)

    if not force and not config.enabled:
        result = {**base, "ok": False, "reason": "disabled"}
        with _state_lock:
            _last_result = result
        return result

    if config.pause_active_tasks and active_tasks and not ignore_active_tasks:
        result = {**base, "ok": False, "skipped": True, "reason": "active_tasks"}
        with _state_lock:
            _last_result = result
        return result

    if not _run_lock.acquire(blocking=False):
        return {**base, "ok": False, "skipped": True, "reason": "already_running"}

    try:
        now = _now()
        with _state_lock:
            _last_run_at = now
            _last_error = ""

        client = _make_tempmail_client(config)
        mailbox_account = client.find_mailbox_by_email(config.mailbox)
        if mailbox_account is None or not getattr(mailbox_account, "account_id", ""):
            raise RuntimeError(f"TempMail 远端未找到邮箱: {config.mailbox}")
        mailbox_id = str(mailbox_account.account_id)
        mailbox_email = str(getattr(mailbox_account, "email", "") or config.mailbox).strip() or config.mailbox

        emails = _list_all_emails(client, mailbox_id)
        email_count = len(emails)
        if not force and email_count < config.threshold:
            next_run_at = _schedule_next(config)
            result = {
                **base,
                "ok": True,
                "skipped": True,
                "reason": "below_threshold",
                "mailbox_id": mailbox_id,
                "email_count": email_count,
                "next_run_at": _iso(next_run_at),
            }
            with _state_lock:
                _last_result = result
            return result

        backup_path = _resolve_backup_path(config.backup_path)
        cutoff_ts = now - config.keep_recent_minutes * 60
        archived = 0
        deleted = 0
        kept_recent = 0
        kept_unknown_time = 0
        archive_errors: list[str] = []
        delete_errors: list[str] = []
        candidates: list[tuple[str, float | None]] = []
        backup_at = datetime.now(timezone.utc).isoformat()

        with _open_archive_db(backup_path) as conn:
            for index, summary in enumerate(emails):
                email_id = _message_id(summary, index)
                try:
                    detail = client._get_email_detail(mailbox_id, email_id)
                    _archive_email(
                        conn,
                        mailbox_email=mailbox_email,
                        mailbox_id=mailbox_id,
                        email_id=email_id,
                        summary=summary,
                        detail=detail,
                        backup_at=backup_at,
                    )
                    conn.commit()
                    archived += 1
                except Exception as exc:
                    archive_errors.append(f"{email_id}: {exc}")
                    continue

                merged = _merge_email_payload(summary, detail)
                received_ts = _parse_message_timestamp(merged)
                if received_ts is None:
                    kept_unknown_time += 1
                    continue
                if received_ts >= cutoff_ts:
                    kept_recent += 1
                    continue
                candidates.append((email_id, received_ts))

            if delete:
                for email_id, _received_ts in candidates:
                    try:
                        ok, status = _delete_email(client, mailbox_id, email_id)
                    except Exception as exc:
                        delete_errors.append(f"{email_id}: {exc}")
                        continue
                    if ok:
                        _mark_deleted(conn, mailbox_id=mailbox_id, email_id=email_id, status=status)
                        conn.commit()
                        deleted += 1
                    else:
                        delete_errors.append(f"{email_id}: {status}")

        next_run_at = _schedule_next(config)
        result = {
            **base,
            "ok": not archive_errors and not delete_errors,
            "reason": "completed",
            "mailbox_id": mailbox_id,
            "email_count": email_count,
            "archived": archived,
            "delete_candidates": len(candidates),
            "deleted": deleted,
            "kept_recent": kept_recent,
            "kept_unknown_time": kept_unknown_time,
            "archive_errors": archive_errors[:20],
            "delete_errors": delete_errors[:20],
            "backup_path": str(backup_path),
            "next_run_at": _iso(next_run_at),
        }
        with _state_lock:
            _last_success_at = _now()
            _last_error = "; ".join((archive_errors + delete_errors)[:3])
            _last_result = result
        print(
            "[TempMail ArchiveCleanup] 完成: "
            f"{mailbox_email} 扫描 {email_count} 封，归档 {archived} 封，删除 {deleted} 封，"
            f"保留最近 {kept_recent} 封，备份 {backup_path}"
        )
        return result
    except Exception as exc:
        message = str(exc)
        _record_error(message)
        _schedule_next(config)
        result = {**base, "ok": False, "reason": "error", "error": message}
        with _state_lock:
            _last_result = result
        print(f"[TempMail ArchiveCleanup] 执行失败: {message}")
        return result
    finally:
        _run_lock.release()


def get_status() -> dict[str, Any]:
    config = get_tempmail_archive_cleanup_config()
    active_tasks = _active_task_snapshots()
    with _state_lock:
        next_run_at = _next_run_at
        last_run_at = _last_run_at
        last_success_at = _last_success_at
        last_error = _last_error
        last_result = dict(_last_result or {})
        running = _running

    now = _now()
    return {
        "running": running,
        "enabled": config.enabled,
        "mailbox": config.mailbox,
        "backup_path": config.backup_path,
        "interval_minutes": config.interval_minutes,
        "keep_recent_minutes": config.keep_recent_minutes,
        "threshold": config.threshold,
        "pause_active_tasks": config.pause_active_tasks,
        "active_task_count": len(active_tasks),
        "next_run_at": _iso(next_run_at),
        "seconds_until_next_run": max(int(next_run_at - now), 0) if next_run_at else 0,
        "last_run_at": _iso(last_run_at),
        "last_success_at": _iso(last_success_at),
        "last_error": last_error,
        "last_result": last_result,
    }


def _loop() -> None:
    global _running

    while not _stop_event.is_set():
        try:
            config = get_tempmail_archive_cleanup_config()
            if config.enabled:
                _ensure_initial_next_run(config)
                with _state_lock:
                    next_run_at = _next_run_at
                if next_run_at and _now() >= next_run_at:
                    run_once()
            else:
                with _state_lock:
                    pass
        except Exception as exc:
            _record_error(str(exc))
            print(f"[TempMail ArchiveCleanup] 调度错误: {exc}")

        _stop_event.wait(LOOP_INTERVAL_SECONDS)

    with _state_lock:
        _running = False


def start() -> None:
    global _worker_thread, _running

    with _state_lock:
        if _running:
            return
        _running = True
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_loop, daemon=True, name="tempmail-archive-cleanup")
    _worker_thread.start()
    print("[TempMail ArchiveCleanup] 已启动")


def stop() -> None:
    global _worker_thread

    _stop_event.set()
    thread = _worker_thread
    if thread and thread.is_alive():
        thread.join(timeout=5)
    _worker_thread = None
    print("[TempMail ArchiveCleanup] 已停止")
