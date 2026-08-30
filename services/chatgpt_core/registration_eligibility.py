"""Bounded post-registration zero-amount eligibility coordination."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import logging
import threading
from typing import Any, Callable

from core.task_runtime import StopTaskRequested, TaskInterruption
from services.chatgpt_core.payment_eligibility import (
    PROFILE,
    ZERO_AMOUNT_KIND,
    payment_eligibility_profile,
    payment_eligibility_stage_regions,
    normalize_checkout_transport,
)
from services.chatgpt_core.task_logging import (
    mask_email_for_log,
    sanitize_error_message,
    sanitize_task_detail,
)


logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 2
DEFAULT_STOP_GRACE_SECONDS = 2.0
_CAPACITY_POLL_SECONDS = 0.1
_PROCESS_CAPACITY = threading.BoundedSemaphore(DEFAULT_CONCURRENCY)


class RegistrationEligibilityCoordinator:
    """Run saved-account probes without occupying registration worker slots."""

    def __init__(
        self,
        *,
        task_id: str,
        settings: dict[str, Any],
        run_account: Callable[..., dict[str, Any]],
        update_meta: Callable[[dict[str, Any]], None],
        log: Callable[[str, str], None],
        on_result: Callable[[dict[str, Any]], None] | None = None,
        stop_checker: Callable[[], None] | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self.task_id = str(task_id or "")
        self.settings = dict(settings or {})
        self.run_account = run_account
        self.update_meta = update_meta
        self.log = log
        self.on_result = on_result
        self.stop_checker = stop_checker
        self.concurrency = max(
            1,
            min(int(concurrency or DEFAULT_CONCURRENCY), DEFAULT_CONCURRENCY),
        )
        try:
            max_attempts = int(self.settings.get("max_attempts") or 2)
        except (TypeError, ValueError):
            max_attempts = 2
        self.max_attempts = max(1, min(max_attempts, 4))
        self.settings["max_attempts"] = self.max_attempts
        self._lock = threading.RLock()
        self._executor: ThreadPoolExecutor | None = None
        self._executor_error = ""
        try:
            self._executor = ThreadPoolExecutor(
                max_workers=self.concurrency,
                thread_name_prefix="registration-zero-amount",
            )
        except Exception as exc:
            self._executor_error = sanitize_error_message(
                str(exc or "后处理线程池不可用")
            )
        self._account_ids: set[int] = set()
        self._results: list[dict[str, Any]] = []
        self._futures: dict[Future[Any], tuple[int, str]] = {}
        self._cleanup_futures: set[Future[Any]] = set()
        self._running_account_ids: set[int] = set()
        self._abandoned_account_ids: set[int] = set()
        self._stop_event = threading.Event()
        self._closing = False
        self._finished = False
        self._counts = {
            "queued": 0,
            "running": 0,
            "eligible": 0,
            "ineligible": 0,
            "probe_failed": 0,
            "pending_auth": 0,
            "skipped": 0,
            "completed": 0,
        }
        self._publish()

    def _snapshot(self) -> dict[str, Any]:
        with self._lock:
            effective_profile = payment_eligibility_profile(
                ZERO_AMOUNT_KIND,
                self.settings,
            )
            return {
                "enabled": True,
                "kind": ZERO_AMOUNT_KIND,
                "profile": {
                    "plan": effective_profile["plan"],
                    "billing_country": effective_profile["billing_country"],
                    "currency": effective_profile["currency"],
                    "promotion": PROFILE["promotion"],
                    "proxy_chain": payment_eligibility_stage_regions(
                        ZERO_AMOUNT_KIND,
                        self.settings,
                    ),
                },
                "effective_concurrency": self.concurrency,
                "global_concurrency_limit": DEFAULT_CONCURRENCY,
                "max_attempts": self.max_attempts,
                "checkout_transport": normalize_checkout_transport(
                    self.settings.get("checkout_transport"),
                    default="browser",
                ),
                "submitted": len(self._account_ids),
                "finished": self._finished,
                "stop_requested": self._stop_event.is_set(),
                "cleanup_pending": len(self._cleanup_futures),
                "counts": dict(self._counts),
                "results": sanitize_task_detail(list(self._results[-500:])),
            }

    def _publish(self) -> None:
        try:
            self.update_meta(self._snapshot())
        except Exception:
            logger.warning(
                "registration eligibility meta update failed task_id=%s",
                self.task_id,
                exc_info=True,
            )

    def _log_safely(self, message: str, level: str = "info") -> None:
        try:
            self.log(message, level)
        except Exception:
            logger.warning(
                "registration eligibility log callback failed task_id=%s",
                self.task_id,
                exc_info=True,
            )

    def _checkpoint(self) -> None:
        if self._stop_event.is_set():
            raise StopTaskRequested()
        if self.stop_checker is not None:
            self.stop_checker()

    @staticmethod
    def _interrupted_result(account_id: int, email: str) -> dict[str, Any]:
        return {
            "account_id": account_id,
            "email": email,
            "status": "skipped",
            "state": "skipped",
            "reason_code": "registration_task_stopped",
            "message": "注册任务已停止，0 元资格检测已取消",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    def _future_done(self, future: Future[Any]) -> None:
        publish = False
        with self._lock:
            self._futures.pop(future, None)
            if future in self._cleanup_futures:
                self._cleanup_futures.discard(future)
                publish = self._finished
        if publish:
            self._publish()

    @staticmethod
    def _failure_result(account_id: int, email: str, exc: Exception) -> dict[str, Any]:
        error_text = sanitize_error_message(str(exc or "检测失败"))
        return {
            "account_id": account_id,
            "email": email,
            "status": "failed",
            "state": "probe_failed",
            "reason_code": "task_exception",
            "message": error_text,
            "error": error_text,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    def _record_result(
        self,
        account_id: int,
        email: str,
        result: Any,
        *,
        was_running: bool,
        emit_log: bool = True,
        notify_result: bool = True,
    ) -> None:
        if not isinstance(result, dict):
            result = self._failure_result(
                account_id,
                email,
                TypeError("资格检测返回了无效结果"),
            )
        state = str(result.get("state") or "probe_failed").strip().lower()
        if state not in {"eligible", "ineligible", "probe_failed", "pending_auth", "skipped"}:
            result = self._failure_result(
                account_id,
                email,
                ValueError(f"资格检测返回了未知状态: {state or '-'}"),
            )
            state = "probe_failed"
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        compact_result = {
            "account_id": account_id,
            "email": str(result.get("email") or email or ""),
            "state": state,
            "reason_code": str(result.get("reason_code") or ""),
            "message": sanitize_error_message(
                str(result.get("message") or result.get("error") or "")
            )[:500],
            "checked_at": str(result.get("checked_at") or ""),
            "amount_minor": evidence.get("amount_minor"),
            "minor_unit_exponent": evidence.get("minor_unit_exponent"),
            "amount_display": str(evidence.get("amount_display") or ""),
            "currency": str(evidence.get("currency") or ""),
            "verified_stage": str(evidence.get("verified_stage") or ""),
            "transport": str(evidence.get("transport") or self.settings.get("checkout_transport") or "browser"),
        }
        with self._lock:
            if was_running:
                self._counts["running"] = max(0, self._counts["running"] - 1)
                self._running_account_ids.discard(account_id)
            self._counts[state] += 1
            self._counts["completed"] += 1
            self._results.append(compact_result)
        self._publish()

        label = {
            "eligible": "0 元可用",
            "ineligible": "非 0 元",
            "probe_failed": "检测失败",
            "pending_auth": "待补 Auth",
            "skipped": "已跳过",
        }[state]
        level = "warning" if state in {"probe_failed", "pending_auth", "skipped"} else "info"
        result_detail = f"｜原因码={compact_result['reason_code'] or '-'}"
        if state == "probe_failed":
            error_response = " ".join(compact_result["message"].split())[:500]
            result_detail = f"｜报错响应={error_response or '上游未返回错误详情'}"
        if emit_log:
            self._log_safely(
                f"[0 元试用资格] 完成｜账号={mask_email_for_log(email) or account_id}"
                f"｜结果={label}{result_detail}",
                level,
            )
        if notify_result and self.on_result is not None:
            try:
                self.on_result(dict(compact_result))
            except Exception:
                logger.warning(
                    "registration eligibility result callback failed task_id=%s account_id=%s",
                    self.task_id,
                    account_id,
                    exc_info=True,
                )

    def submit(self, account_id: Any, email: str = "") -> bool:
        try:
            account_id_value = int(account_id or 0)
        except (TypeError, ValueError):
            account_id_value = 0
        if account_id_value <= 0:
            return False

        future: Future[Any] | None = None
        submit_error: Exception | None = None
        with self._lock:
            executor = self._executor
            if (
                self._closing
                or self._finished
                or account_id_value in self._account_ids
            ):
                return False
            self._account_ids.add(account_id_value)
            self._counts["queued"] += 1
            if executor is not None:
                try:
                    future = executor.submit(
                        self._run,
                        account_id_value,
                        str(email or ""),
                    )
                    self._futures[future] = (
                        account_id_value,
                        str(email or ""),
                    )
                except Exception as exc:
                    submit_error = exc
        self._publish()
        if executor is None:
            with self._lock:
                self._counts["queued"] = max(0, self._counts["queued"] - 1)
            error_text = self._executor_error or "后处理线程池不可用"
            self._log_safely(
                "[0 元试用资格] 后处理线程池不可用，记录为检测失败｜"
                f"账号={mask_email_for_log(str(email or '')) or account_id_value}｜原因={error_text}",
                "warning",
            )
            self._record_configuration_failure(account_id_value, str(email or ""), error_text)
            return True
        if submit_error is not None:
            with self._lock:
                self._counts["queued"] = max(0, self._counts["queued"] - 1)
            error_text = sanitize_error_message(
                str(submit_error or "后处理入队失败")
            )
            self._log_safely(
                "[0 元试用资格] 后处理入队失败，记录为检测失败｜"
                f"账号={mask_email_for_log(str(email or '')) or account_id_value}｜原因={error_text}",
                "warning",
            )
            self._record_configuration_failure(
                account_id_value,
                str(email or ""),
                f"后处理入队失败: {error_text}",
            )
        elif future is not None:
            future.add_done_callback(self._future_done)
        return True

    def _record_configuration_failure(
        self,
        account_id: int,
        email: str,
        error_text: str,
    ) -> None:
        try:
            result = self.run_account(
                account_id,
                ZERO_AMOUNT_KIND,
                {
                    **self.settings,
                    "_configuration_error": error_text,
                },
                task_id=self.task_id,
            )
        except Exception as persist_exc:
            result = self._failure_result(account_id, email, persist_exc)
        self._record_result(
            account_id,
            email,
            result,
            was_running=False,
        )

    def _run(self, account_id: int, email: str) -> None:
        capacity_acquired = False
        was_running = False
        notify_result = True
        try:
            while not _PROCESS_CAPACITY.acquire(timeout=_CAPACITY_POLL_SECONDS):
                self._checkpoint()
            capacity_acquired = True
            self._checkpoint()
            with self._lock:
                self._counts["queued"] = max(0, self._counts["queued"] - 1)
                self._counts["running"] += 1
                self._running_account_ids.add(account_id)
            was_running = True
            self._publish()
            self._log_safely(
                f"[0 元试用资格] 开始｜账号={mask_email_for_log(email) or account_id}",
                "info",
            )
            result = self.run_account(
                account_id,
                ZERO_AMOUNT_KIND,
                self.settings,
                task_id=self.task_id,
                stop_checker=self._checkpoint,
            )
            self._checkpoint()
        except TaskInterruption:
            result = self._interrupted_result(account_id, email)
            notify_result = False
        except Exception as exc:
            result = self._failure_result(account_id, email, exc)
        finally:
            if capacity_acquired:
                _PROCESS_CAPACITY.release()
        if not was_running:
            with self._lock:
                self._counts["queued"] = max(0, self._counts["queued"] - 1)
        with self._lock:
            if account_id in self._abandoned_account_ids:
                return
        self._record_result(
            account_id,
            email,
            result,
            was_running=was_running,
            notify_result=notify_result,
        )

    def finish(
        self,
        *,
        cancel_pending: bool = False,
        stop_grace_seconds: float = DEFAULT_STOP_GRACE_SECONDS,
    ) -> dict[str, Any]:
        publish_without_executor = False
        with self._lock:
            executor = self._executor
            if executor is None:
                if cancel_pending:
                    self._stop_event.set()
                if not self._closing and not self._finished:
                    self._finished = True
                    publish_without_executor = True
                futures: dict[Future[Any], tuple[int, str]] = {}
            else:
                self._executor = None
                self._closing = True
                futures = dict(self._futures)

        if executor is None:
            if publish_without_executor:
                self._publish()
            return self._snapshot()

        cancelled: list[tuple[int, str]] = []
        cleanup_pending: set[Future[Any]] = set()
        if cancel_pending:
            # Cancel queued work before waking the active worker with the stop
            # event. This closes the race where the executor could otherwise
            # start the next account while the current probe is unwinding.
            for future, item in futures.items():
                if future.cancel():
                    cancelled.append(item)
            self._stop_event.set()
            for account_id, email in cancelled:
                with self._lock:
                    self._counts["queued"] = max(0, self._counts["queued"] - 1)
                self._record_result(
                    account_id,
                    email,
                    self._interrupted_result(account_id, email),
                    was_running=False,
                    emit_log=False,
                    notify_result=False,
                )
            executor.shutdown(wait=False, cancel_futures=True)
            active_futures = [
                future
                for future in futures
                if not future.cancelled() and not future.done()
            ]
            if active_futures:
                _, cleanup_pending = wait(
                    active_futures,
                    timeout=max(float(stop_grace_seconds or 0.0), 0.0),
                )
        else:
            executor.shutdown(wait=True, cancel_futures=False)

        with self._lock:
            self._cleanup_futures = {
                future for future in cleanup_pending if not future.done()
            }
            abandoned_items = [
                futures[future]
                for future in self._cleanup_futures
                if future in futures
            ]
            for account_id, _email in abandoned_items:
                self._abandoned_account_ids.add(account_id)
            self._finished = True
            self._closing = False
        for account_id, email in abandoned_items:
            with self._lock:
                was_running = account_id in self._running_account_ids
                if not was_running:
                    self._counts["queued"] = max(0, self._counts["queued"] - 1)
            self._record_result(
                account_id,
                email,
                self._interrupted_result(account_id, email),
                was_running=was_running,
                emit_log=False,
                notify_result=False,
            )
        with self._lock:
            counts = dict(self._counts)
            cleanup_count = len(self._cleanup_futures)
        self._publish()
        if cancel_pending:
            self._log_safely(
                "[0 元试用资格] 停止｜"
                f"已取消排队={len(cancelled)}｜后台清理中={cleanup_count}",
                "warning" if cleanup_count else "info",
            )
        if counts["completed"]:
            self._log_safely(
                "[0 元试用资格] 汇总｜"
                f"0 元可用={counts['eligible']}｜非 0 元={counts['ineligible']}｜"
                f"检测失败={counts['probe_failed']}｜待补 Auth={counts['pending_auth']}｜"
                f"已跳过={counts['skipped']}",
                "info",
            )
        return self._snapshot()
