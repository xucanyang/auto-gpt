"""iCloud HME 未使用/失效别名自动清理。

与 ``icloud_hme_auto_pool`` 互补：补池负责按随机延时「创建」别名，本 worker 负责
按随机延时「删除」那些没有挂在存活 ChatGPT 账号上的别名，在 Apple 端 deactivate→delete。

两段式（用户确认）：
  1. 失效测活：对「绑定了别名、且当前 status=invalid」的 chatgpt 账号复用
     ``recheck_invalid_chatgpt_account`` 再测一遍，能恢复的自动恢复（→保留）。
  2. 扫删：删除「孤儿别名（不在任何 chatgpt 账号里）」+「绑定账号经测活后仍确定性失效」
     的别名，逐个之间随机延时，遵守限流退避与普通错误短退避。

安全：
  - 永不删「在途」(status=in_use / task_id 非空) 与「待用库存」(reserved+enabled+未绑定)；
    分类逻辑见 ``core.db.list_icloud_hme_deletion_candidates``。
  - 失效测活临时失败（网络/限流/不可测）绝不当作失效，留到下轮。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import random
import threading
import time
from typing import Any


DEFAULT_ACCOUNT_INTERVAL_MINUTES_MIN = 10
DEFAULT_ACCOUNT_INTERVAL_MINUTES_MAX = 30
DEFAULT_SCAN_INTERVAL_SECONDS = 60
DEFAULT_MAX_PER_RUN = 20
DEFAULT_RATE_LIMIT_BACKOFF_MINUTES = 60
DEFAULT_ERROR_BACKOFF_MINUTES = 3
DEFAULT_ERROR_BACKOFF_MAX_MINUTES = 15
DEFAULT_DEAD_STATUSES = "account_deactivated,password_invalid"
LOOP_INTERVAL_SECONDS = 60

_state_lock = threading.Lock()
_run_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_running = False
_next_run_at = 0.0
_rate_limit_until = 0.0
_error_backoff_until = 0.0
_consecutive_error_count = 0
_last_run_at = 0.0
_last_success_at = 0.0
_last_error = ""
_last_backoff_reason = ""
_last_result: dict[str, Any] = {}


@dataclass
class IcloudHmeAutoDeleteConfig:
    enabled: bool
    account_interval_min_minutes: int
    account_interval_max_minutes: int
    max_per_run: int
    rate_limit_backoff_minutes: int
    error_backoff_minutes: int
    recheck_before_delete: bool
    pause_active_tasks: bool
    dead_statuses: set[str]
    icloud_cookie: str
    icloud_domain_base: str
    purpose: str = "chatgpt_register"
    bound_service: str = "chatgpt"


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


def _parse_dead_statuses(value: str | None) -> set[str]:
    raw = str(value or "").strip()
    if not raw:
        return {s.strip().lower() for s in DEFAULT_DEAD_STATUSES.split(",") if s.strip()}
    parts = {p.strip().lower() for p in raw.replace(" ", ",").split(",") if p.strip()}
    return parts or {s.strip().lower() for s in DEFAULT_DEAD_STATUSES.split(",") if s.strip()}


def _now() -> float:
    return time.time()


def _iso(timestamp: float) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def get_icloud_hme_auto_delete_config() -> IcloudHmeAutoDeleteConfig:
    store = _config_store()
    min_minutes = _to_int(
        store.get("icloud_hme_auto_delete_account_interval_min_minutes", ""),
        DEFAULT_ACCOUNT_INTERVAL_MINUTES_MIN,
        minimum=0,
    )
    max_minutes = _to_int(
        store.get("icloud_hme_auto_delete_account_interval_max_minutes", ""),
        DEFAULT_ACCOUNT_INTERVAL_MINUTES_MAX,
        minimum=0,
    )
    if max_minutes < min_minutes:
        max_minutes = min_minutes

    return IcloudHmeAutoDeleteConfig(
        enabled=_to_bool(store.get("icloud_hme_auto_delete_enabled", ""), default=False),
        account_interval_min_minutes=min_minutes,
        account_interval_max_minutes=max_minutes,
        max_per_run=_to_int(
            store.get("icloud_hme_auto_delete_max_per_run", ""),
            DEFAULT_MAX_PER_RUN,
            minimum=1,
        ),
        rate_limit_backoff_minutes=_to_int(
            store.get("icloud_hme_auto_delete_rate_limit_backoff_minutes", ""),
            DEFAULT_RATE_LIMIT_BACKOFF_MINUTES,
            minimum=1,
        ),
        error_backoff_minutes=_to_int(
            store.get("icloud_hme_auto_delete_error_backoff_minutes", ""),
            DEFAULT_ERROR_BACKOFF_MINUTES,
            minimum=0,
        ),
        recheck_before_delete=_to_bool(
            store.get("icloud_hme_auto_delete_recheck_before_delete", ""),
            default=True,
        ),
        pause_active_tasks=_to_bool(
            store.get("icloud_hme_auto_delete_pause_active_tasks", ""),
            default=True,
        ),
        dead_statuses=_parse_dead_statuses(store.get("icloud_hme_auto_delete_dead_statuses", "")),
        icloud_cookie=str(store.get("icloud_cookie", "") or "").strip(),
        icloud_domain_base=str(store.get("icloud_domain_base", "icloud.com") or "icloud.com").strip()
        or "icloud.com",
    )


def _schedule_next(config: IcloudHmeAutoDeleteConfig, *, base_timestamp: float | None = None) -> float:
    global _next_run_at

    next_run_at = float(base_timestamp if base_timestamp is not None else _now()) + DEFAULT_SCAN_INTERVAL_SECONDS
    with _state_lock:
        _next_run_at = next_run_at
    return next_run_at


def _ensure_initial_next_run(config: IcloudHmeAutoDeleteConfig) -> None:
    with _state_lock:
        current_next_run = _next_run_at
    if current_next_run > 0:
        return
    _schedule_next(config)


def _record_error(message: str) -> None:
    global _last_error

    with _state_lock:
        _last_error = str(message or "").strip()


def _set_error_backoff(config: IcloudHmeAutoDeleteConfig, message: str) -> tuple[float, int]:
    global _error_backoff_until, _next_run_at, _last_error, _consecutive_error_count, _last_backoff_reason

    with _state_lock:
        _consecutive_error_count = max(int(_consecutive_error_count or 0), 0) + 1
        if config.error_backoff_minutes <= 0:
            wait_minutes = 0
            _error_backoff_until = 0.0
        else:
            wait_minutes = min(
                config.error_backoff_minutes * _consecutive_error_count,
                DEFAULT_ERROR_BACKOFF_MAX_MINUTES,
            )
            _error_backoff_until = _now() + wait_minutes * 60
            _next_run_at = _error_backoff_until
        _last_error = str(message or "").strip()
        _last_backoff_reason = "普通错误短退避" if wait_minutes > 0 else ""
        return _error_backoff_until, wait_minutes


def _clear_error_backoff() -> None:
    global _error_backoff_until, _consecutive_error_count, _last_backoff_reason

    with _state_lock:
        _error_backoff_until = 0.0
        _consecutive_error_count = 0
        _last_backoff_reason = ""


def _is_non_retryable_error(exc: Exception, message: str) -> bool:
    try:
        from core.base_mailbox import ICloudAuthExpiredError

        if isinstance(exc, ICloudAuthExpiredError):
            return True
    except Exception:
        pass
    text = str(message or "").lower()
    return any(marker in text for marker in ("未配置", "cookie 已失效", "invalid cookie", "session expired"))


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


def _make_client(config: IcloudHmeAutoDeleteConfig):
    from core.base_mailbox import ICloudHmeClient

    return ICloudHmeClient(
        cookie=config.icloud_cookie,
        domain_base=config.icloud_domain_base,
        proxy=None,
    )


def _classify_recheck_result(result: Any, dead_statuses: set[str]) -> str:
    """把失效测活返回映射为 'alive' | 'dead' | 'keep'。

    - ok=True            → alive（已恢复/可登录，保留）
    - error_code 命中死号集合（默认 account_deactivated / password_invalid） → dead（可删）
    - 其余（network_failed/login_blocked/unknown/不可测/异常） → keep（临时或不确定，不删）
    """
    if not isinstance(result, dict):
        return "keep"
    if result.get("ok"):
        return "alive"
    data = result.get("data") or {}
    error_code = str(data.get("error_code") or "").strip().lower()
    if error_code in dead_statuses:
        return "dead"
    return "keep"


def _create_probe_account(hme: str, anonymous_id: str) -> int | None:
    """为孤儿别名建立一个临时 invalid 账号行，以复用 recheck 的免密登录测活。

    recheck 的 ``_mailbox_state_from_account`` 会按 ``mail_provider==icloud_hme`` + 全局配置
    自动重建收件邮箱（并按 hme 查 anonymous_id），所以这里只需最小字段。
    """
    from core.db import AccountModel, engine
    from sqlmodel import Session

    email = str(hme or "").strip()
    if not email:
        return None
    extra = {"mail_provider": "icloud_hme", "anonymous_id": str(anonymous_id or "").strip()}
    try:
        with Session(engine) as session:
            row = AccountModel(
                platform="chatgpt",
                email=email,
                password="",
                status="invalid",
                extra_json=json.dumps(extra, ensure_ascii=False),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)
    except Exception as exc:
        print(f"[iCloudHME AutoDelete] 建立临时探测账号失败 {email}: {exc}")
        return None


def _delete_probe_account(account_id: int) -> None:
    from core.db import AccountModel, engine
    from sqlmodel import Session

    try:
        with Session(engine) as session:
            row = session.get(AccountModel, int(account_id or 0))
            if row is not None:
                session.delete(row)
                session.commit()
    except Exception as exc:
        print(f"[iCloudHME AutoDelete] 清理临时探测账号失败 #{account_id}: {exc}")


def _probe_account_ids(account_ids: list[int], recheck_fn, dead_statuses: set[str]) -> str:
    """对一组账号逐个免密登录测活，聚合判定：任一存活→alive；否则任一确认失效→dead；其余→skip。"""
    verdicts: list[str] = []
    for aid in account_ids:
        if not aid:
            continue
        try:
            result = recheck_fn(int(aid), task_id="icloud_hme_auto_delete")
        except Exception as exc:
            print(f"[iCloudHME AutoDelete] 测活异常 account#{aid}: {exc}")
            verdicts.append("keep")
            continue
        verdicts.append(_classify_recheck_result(result, dead_statuses))
    if "alive" in verdicts:
        return "alive"
    if "dead" in verdicts:
        return "dead"
    return "skip"


def _probe_candidate(cand: dict[str, Any], recheck_fn, dead_statuses: set[str]) -> str:
    """对单个删除候选做删前测活。
    - bound_invalid：直接对已有账号 recheck（存活则被恢复）。
    - orphan：临时建账号行复用 recheck（存活则其 ``_persist_recheck_success`` 自动重新导入；
      非存活则清理临时行）。
    返回 'alive' | 'dead' | 'skip'。
    """
    if cand.get("kind") == "bound_invalid":
        return _probe_account_ids(list(cand.get("account_ids") or []), recheck_fn, dead_statuses)

    temp_id = _create_probe_account(cand.get("hme", ""), cand.get("anonymous_id", ""))
    if not temp_id:
        return "skip"
    verdict = "skip"
    try:
        verdict = _probe_account_ids([temp_id], recheck_fn, dead_statuses)
    finally:
        # 存活的孤儿已被 recheck 恢复成正式账号（重新导入），保留该行；其余清理临时行。
        if verdict != "alive":
            _delete_probe_account(temp_id)
    return verdict


def _build_base_result(config: IcloudHmeAutoDeleteConfig) -> dict[str, Any]:
    return {
        "account_interval_min_minutes": config.account_interval_min_minutes,
        "account_interval_max_minutes": config.account_interval_max_minutes,
        "max_per_run": config.max_per_run,
        "rate_limit_backoff_minutes": config.rate_limit_backoff_minutes,
        "error_backoff_minutes": config.error_backoff_minutes,
        "recheck_before_delete": config.recheck_before_delete,
        "pause_active_tasks": config.pause_active_tasks,
        "dead_statuses": sorted(config.dead_statuses),
    }


def run_once(
    *,
    force: bool = False,
    ignore_active_tasks: bool = False,
    delete: bool = True,
) -> dict[str, Any]:
    global _last_run_at, _last_success_at, _last_error, _last_result, _rate_limit_until, _next_run_at
    global _error_backoff_until, _consecutive_error_count, _last_backoff_reason

    from core.base_mailbox import ICloudAliasLimitError, ICloudAuthExpiredError
    from core.db import list_icloud_hme_deletion_candidates, mark_icloud_hme_alias_retired

    config = get_icloud_hme_auto_delete_config()
    base = _build_base_result(config)
    active_tasks = _active_task_snapshots()
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

    now = _now()
    with _state_lock:
        rate_limited = _rate_limit_until and now < _rate_limit_until
        error_backoff = _error_backoff_until and now < _error_backoff_until
    if not force and rate_limited:
        result = {**base, "ok": False, "reason": "rate_limit_backoff", "rate_limit_until": _iso(_rate_limit_until)}
        with _state_lock:
            _last_result = result
        return result
    if not force and error_backoff:
        result = {**base, "ok": False, "reason": "error_backoff", "error_backoff_until": _iso(_error_backoff_until)}
        with _state_lock:
            _last_result = result
        return result

    if not _run_lock.acquire(blocking=False):
        return {**base, "ok": False, "skipped": True, "reason": "already_running"}

    try:
        with _state_lock:
            _last_run_at = now
            _last_error = ""

        if delete and not config.icloud_cookie:
            raise RuntimeError("iCloud HME 自动删除未配置 icloud_cookie")

        analysis = list_icloud_hme_deletion_candidates(
            purpose=config.purpose,
            bound_service=config.bound_service,
        )
        orphan = analysis.get("orphan", [])
        bound_invalid = analysis.get("bound_invalid", [])

        # 统一候选：孤儿(无账号) + 失效绑定(带 account_ids)。两类删前都要先免密登录测活。
        candidates: list[dict[str, Any]] = []
        for item in orphan:
            anon = str(item.get("anonymous_id") or "").strip()
            if anon:
                candidates.append({"anonymous_id": anon, "hme": str(item.get("hme") or ""), "kind": "orphan", "account_ids": [], "recheck_confirmed": bool(item.get("recheck_confirmed"))})
        for item in bound_invalid:
            anon = str(item.get("anonymous_id") or "").strip()
            if anon:
                candidates.append({"anonymous_id": anon, "hme": str(item.get("hme") or ""), "kind": "bound_invalid", "account_ids": list(item.get("account_ids", [])), "recheck_confirmed": bool(item.get("recheck_confirmed"))})

        candidate_total = len(candidates)
        to_process = candidates[: config.max_per_run]
        capped = candidate_total - len(to_process)

        recheck_fn = None
        if config.recheck_before_delete:
            from services.chatgpt_core.invalid_account_recheck import (
                recheck_invalid_chatgpt_account as recheck_fn,
            )

        client = _make_client(config) if delete else None
        rechecked = 0
        deleted = 0
        kept_alive = 0
        skipped = 0
        recheck_errors: list[str] = []
        delete_errors: list[str] = []
        rate_limited_hit = False
        ordinary_error_hit = False
        ordinary_error_message = ""
        auth_expired = False

        for index, cand in enumerate(to_process):
            if _stop_event.is_set():
                break
            if index > 0:
                delay = random.uniform(
                    config.account_interval_min_minutes * 60,
                    config.account_interval_max_minutes * 60,
                )
                if delay > 0 and _stop_event.wait(delay):
                    break

            anon = cand["anonymous_id"]
            hme = cand["hme"]

            # ---- 删前测活：免密登录探测（孤儿临时建账号行复用 recheck，存活自动重新导入）----
            if recheck_fn is not None:
                try:
                    verdict = _probe_candidate(cand, recheck_fn, config.dead_statuses)
                    rechecked += 1
                except Exception as exc:
                    verdict = "skip"
                    ordinary_error_hit = True
                    ordinary_error_message = f"{hme or anon}: probe error {exc}"
                    recheck_errors.append(ordinary_error_message)
            else:
                # 关闭测活：信任既有 invalid 状态，直接当作可删
                verdict = "dead"

            if verdict == "alive":
                kept_alive += 1
                continue
            if verdict != "dead":
                skipped += 1
                if ordinary_error_hit:
                    break
                continue

            # ---- 确认失效 → Apple deactivate+delete + 本地退役 ----
            if not delete:
                deleted += 1  # dry-run 仅计数
                continue
            try:
                try:
                    client.deactivate(anonymous_id=anon)
                except ICloudAliasLimitError:
                    raise
                except ICloudAuthExpiredError:
                    raise
                except Exception as exc:
                    print(f"[iCloudHME AutoDelete] deactivate 告警 {hme or anon}: {exc}")
                client.delete(anonymous_id=anon)
            except ICloudAliasLimitError as exc:
                retry_after = max(int(getattr(exc, "retry_after", 0) or 0), 0)
                backoff_seconds = max(config.rate_limit_backoff_minutes * 60, retry_after)
                with _state_lock:
                    _rate_limit_until = _now() + backoff_seconds
                    _next_run_at = _rate_limit_until
                    _error_backoff_until = 0.0
                    _consecutive_error_count = 0
                    _last_backoff_reason = ""
                rate_limited_hit = True
                print(f"[iCloudHME AutoDelete] 触发 iCloud 限流，延后到 {_iso(_rate_limit_until)} 再试: {exc}")
                break
            except ICloudAuthExpiredError as exc:
                auth_expired = True
                delete_errors.append(f"{hme or anon}: {exc}")
                print(f"[iCloudHME AutoDelete] iCloud cookie 失效，终止本轮: {exc}")
                break
            except Exception as exc:
                ordinary_error_hit = True
                ordinary_error_message = f"{hme or anon}: {exc}"
                delete_errors.append(ordinary_error_message)
                print(f"[iCloudHME AutoDelete] 删除临时失败，进入普通错误短退避: {ordinary_error_message}")
                break

            try:
                mark_icloud_hme_alias_retired(anon, reason=f"auto_delete:{cand['kind']}")
            except Exception as exc:
                print(f"[iCloudHME AutoDelete] 已在 Apple 删除但本地退役失败 {hme or anon}: {exc}")
            deleted += 1

        if rate_limited_hit:
            with _state_lock:
                next_run_at = _rate_limit_until
        elif ordinary_error_hit:
            error_backoff_until, wait_minutes = _set_error_backoff(config, ordinary_error_message)
            next_run_at = error_backoff_until if wait_minutes > 0 else _schedule_next(config)
            if wait_minutes > 0:
                print(f"[iCloudHME AutoDelete] 普通错误短退避 {wait_minutes} 分钟后再试: {ordinary_error_message}")
        else:
            next_run_at = _schedule_next(config)
        ok = not delete_errors and not recheck_errors and not rate_limited_hit and not auth_expired
        result = {
            **base,
            "ok": ok,
            "reason": "completed" if delete else "dry_run",
            "summary": analysis.get("summary", {}),
            "orphan_count": len(orphan),
            "bound_invalid_count": len(bound_invalid),
            "candidate_total": candidate_total,
            "processed": len(to_process),
            "rechecked": rechecked,
            "deleted": deleted,
            "kept_alive": kept_alive,
            "skipped": skipped,
            "capped": capped,
            "recheck_errors": recheck_errors[:20],
            "delete_errors": delete_errors[:20],
            "rate_limited": rate_limited_hit,
            "ordinary_error": ordinary_error_hit,
            "auth_expired": auth_expired,
            "next_run_at": _iso(next_run_at),
        }
        if ordinary_error_hit:
            result["error_backoff_until"] = _iso(next_run_at)
        if capped > 0:
            print(f"[iCloudHME AutoDelete] 本轮候选 {candidate_total} 个，受单次上限只处理 {len(to_process)} 个，剩余 {capped} 个下轮继续")
        with _state_lock:
            if not rate_limited_hit:
                _rate_limit_until = 0.0
            if not ordinary_error_hit:
                _error_backoff_until = 0.0
                _consecutive_error_count = 0
                _last_backoff_reason = ""
            _last_success_at = _now()
            _last_error = "; ".join((recheck_errors + delete_errors)[:3])
            _last_result = result
        print(
            f"[iCloudHME AutoDelete] 完成：候选 {candidate_total}（孤儿 {len(orphan)} / 失效绑定 {len(bound_invalid)}），"
            f"测活 {rechecked}，存活保留 {kept_alive}，删除 {deleted}，跳过 {skipped}，下次 {_iso(next_run_at)}"
        )
        return result
    except Exception as exc:
        message = str(exc)
        _record_error(message)
        if _is_non_retryable_error(exc, message):
            next_run_at = _schedule_next(config)
            result = {
                **base,
                "ok": False,
                "reason": "non_retryable_error",
                "error": message,
                "next_run_at": _iso(next_run_at),
            }
            with _state_lock:
                _last_result = result
            print(f"[iCloudHME AutoDelete] 执行失败，需要人工处理配置/凭据: {message}")
            return result
        error_backoff_until, wait_minutes = _set_error_backoff(config, message)
        if wait_minutes <= 0:
            _schedule_next(config)
        result = {
            **base,
            "ok": False,
            "reason": "error",
            "error": message,
            "error_backoff_until": _iso(error_backoff_until),
            "error_backoff_minutes": wait_minutes,
        }
        with _state_lock:
            _last_result = result
        if wait_minutes > 0:
            print(f"[iCloudHME AutoDelete] 执行失败，普通错误短退避 {wait_minutes} 分钟后再试: {message}")
        else:
            print(f"[iCloudHME AutoDelete] 执行失败: {message}")
        return result
    finally:
        _run_lock.release()


def get_status() -> dict[str, Any]:
    config = get_icloud_hme_auto_delete_config()
    try:
        from core.db import list_icloud_hme_deletion_candidates

        analysis = list_icloud_hme_deletion_candidates(
            purpose=config.purpose,
            bound_service=config.bound_service,
        )
        summary = analysis.get("summary", {})
        pending_candidates = len(analysis.get("candidates", []))
    except Exception as exc:
        summary = {"error": str(exc)}
        pending_candidates = 0

    with _state_lock:
        next_run_at = _next_run_at
        rate_limit_until = _rate_limit_until
        error_backoff_until = _error_backoff_until
        consecutive_error_count = _consecutive_error_count
        last_run_at = _last_run_at
        last_success_at = _last_success_at
        last_error = _last_error
        last_backoff_reason = _last_backoff_reason
        last_result = dict(_last_result or {})
        running = _running

    now = _now()
    return {
        "running": running,
        "enabled": config.enabled,
        "account_interval_min_minutes": config.account_interval_min_minutes,
        "account_interval_max_minutes": config.account_interval_max_minutes,
        "max_per_run": config.max_per_run,
        "rate_limit_backoff_minutes": config.rate_limit_backoff_minutes,
        "error_backoff_minutes": config.error_backoff_minutes,
        "recheck_before_delete": config.recheck_before_delete,
        "pause_active_tasks": config.pause_active_tasks,
        "dead_statuses": sorted(config.dead_statuses),
        "pending_candidates": pending_candidates,
        "candidate_summary": summary,
        "next_run_at": _iso(next_run_at),
        "seconds_until_next_run": max(int(next_run_at - now), 0) if next_run_at else 0,
        "rate_limit_until": _iso(rate_limit_until),
        "in_rate_limit_backoff": bool(rate_limit_until and now < rate_limit_until),
        "error_backoff_until": _iso(error_backoff_until),
        "in_error_backoff": bool(error_backoff_until and now < error_backoff_until),
        "consecutive_error_count": consecutive_error_count,
        "last_backoff_reason": last_backoff_reason,
        "last_run_at": _iso(last_run_at),
        "last_success_at": _iso(last_success_at),
        "last_error": last_error,
        "last_result": last_result,
    }


def _loop() -> None:
    global _running

    while not _stop_event.is_set():
        try:
            config = get_icloud_hme_auto_delete_config()
            if config.enabled:
                _ensure_initial_next_run(config)
                now = _now()
                with _state_lock:
                    next_run_at = _next_run_at
                    rate_limit_until = _rate_limit_until
                    error_backoff_until = _error_backoff_until
                if rate_limit_until and now < rate_limit_until:
                    pass
                elif error_backoff_until and now < error_backoff_until:
                    pass
                elif next_run_at and now >= next_run_at:
                    run_once()
        except Exception as exc:
            _record_error(str(exc))
            print(f"[iCloudHME AutoDelete] 调度错误: {exc}")

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
    _worker_thread = threading.Thread(target=_loop, daemon=True, name="icloud-hme-auto-delete")
    _worker_thread.start()
    print("[iCloudHME AutoDelete] 已启动")


def stop() -> None:
    global _worker_thread

    _stop_event.set()
    thread = _worker_thread
    if thread and thread.is_alive():
        thread.join(timeout=5)
    _worker_thread = None
    print("[iCloudHME AutoDelete] 已停止")
