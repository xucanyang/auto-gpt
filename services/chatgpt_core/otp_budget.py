"""OTP 等待预算工具。"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class OtpWaitPlan:
    """一次验证码等待的有效超时计划。"""

    timeout_seconds: int
    requested_seconds: int
    remaining_seconds: int | None = None
    exhausted: bool = False

    @property
    def clamped(self) -> bool:
        return self.remaining_seconds is not None and self.timeout_seconds < self.requested_seconds


class RegistrationOtpBudget:
    """单账号注册邮箱验证码累计等待预算。

    预算从第一次真正进入邮箱验证码等待开始计时，只限制当前账号的验证码等待阶段，
    不限制整批任务总耗时。
    """

    def __init__(
        self,
        total_seconds: int,
        *,
        label: str = "单账号注册邮箱验证码",
        clock: Callable[[], float] | None = None,
    ) -> None:
        try:
            parsed = int(total_seconds or 0)
        except (TypeError, ValueError):
            parsed = 0
        self.total_seconds = max(parsed, 0)
        self.label = str(label or "单账号注册邮箱验证码")
        self._clock = clock or time.monotonic
        self._started_at: float | None = None
        self._deadline: float | None = None

    @property
    def active(self) -> bool:
        return self.total_seconds > 0

    @property
    def started(self) -> bool:
        return self._started_at is not None

    def _ensure_started(self) -> None:
        if not self.active or self._started_at is not None:
            return
        now = self._clock()
        self._started_at = now
        self._deadline = now + self.total_seconds

    def remaining_seconds(self) -> int | None:
        if not self.active:
            return None
        self._ensure_started()
        if self._deadline is None:
            return None
        return max(0, int(math.ceil(self._deadline - self._clock())))

    def is_exhausted(self) -> bool:
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= 0

    def plan_wait(self, requested_timeout: int) -> OtpWaitPlan:
        try:
            requested = int(requested_timeout or 0)
        except (TypeError, ValueError):
            requested = 0
        requested = max(requested, 1)

        remaining = self.remaining_seconds()
        if remaining is None:
            return OtpWaitPlan(timeout_seconds=requested, requested_seconds=requested)
        if remaining <= 0:
            return OtpWaitPlan(
                timeout_seconds=0,
                requested_seconds=requested,
                remaining_seconds=0,
                exhausted=True,
            )
        return OtpWaitPlan(
            timeout_seconds=max(1, min(requested, remaining)),
            requested_seconds=requested,
            remaining_seconds=remaining,
            exhausted=False,
        )
