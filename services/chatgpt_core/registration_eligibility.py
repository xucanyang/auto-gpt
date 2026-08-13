"""Bounded post-registration zero-amount eligibility coordination."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
import threading
from typing import Any, Callable

from services.chatgpt_core.payment_eligibility import (
    PROFILE,
    ZERO_AMOUNT_KIND,
    payment_eligibility_profile,
    payment_eligibility_stage_regions,
)
from services.chatgpt_core.task_logging import (
    mask_email_for_log,
    sanitize_error_message,
    sanitize_task_detail,
)


logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 2
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
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self.task_id = str(task_id or "")
        self.settings = dict(settings or {})
        self.run_account = run_account
        self.update_meta = update_meta
        self.log = log
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
                "submitted": len(self._account_ids),
                "finished": self._finished,
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
        }
        with self._lock:
            if was_running:
                self._counts["running"] = max(0, self._counts["running"] - 1)
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
        self._log_safely(
            f"[0 元试用资格] 完成｜账号={mask_email_for_log(email) or account_id}"
            f"｜结果={label}｜原因码={compact_result['reason_code'] or '-'}",
            level,
        )

    def submit(self, account_id: Any, email: str = "") -> bool:
        try:
            account_id_value = int(account_id or 0)
        except (TypeError, ValueError):
            account_id_value = 0
        if account_id_value <= 0:
            return False

        with self._lock:
            executor = self._executor
            if self._finished or account_id_value in self._account_ids:
                return False
            self._account_ids.add(account_id_value)
            self._counts["queued"] += 1
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
        try:
            executor.submit(self._run, account_id_value, str(email or ""))
        except Exception as exc:
            with self._lock:
                self._counts["queued"] = max(0, self._counts["queued"] - 1)
            error_text = sanitize_error_message(str(exc or "后处理入队失败"))
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
        with _PROCESS_CAPACITY:
            with self._lock:
                self._counts["queued"] = max(0, self._counts["queued"] - 1)
                self._counts["running"] += 1
            self._publish()
            self._log_safely(
                f"[0 元试用资格] 开始｜账号={mask_email_for_log(email) or account_id}",
                "info",
            )
            try:
                result = self.run_account(
                    account_id,
                    ZERO_AMOUNT_KIND,
                    self.settings,
                    task_id=self.task_id,
                )
            except Exception as exc:
                result = self._failure_result(account_id, email, exc)
            self._record_result(
                account_id,
                email,
                result,
                was_running=True,
            )

    def finish(self) -> dict[str, Any]:
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)

        with self._lock:
            if not self._finished:
                self._finished = True
            counts = dict(self._counts)
        self._publish()
        if counts["completed"]:
            self._log_safely(
                "[0 元试用资格] 汇总｜"
                f"0 元可用={counts['eligible']}｜非 0 元={counts['ineligible']}｜"
                f"检测失败={counts['probe_failed']}｜待补 Auth={counts['pending_auth']}｜"
                f"已跳过={counts['skipped']}",
                "info",
            )
        return self._snapshot()
