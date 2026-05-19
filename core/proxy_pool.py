"""代理池 - 从数据库读取代理，支持轮询和按区域选取"""

from typing import Optional
from sqlmodel import Session, select
from .db import ProxyModel, engine
from .config_store import config_store
from .proxy_utils import build_requests_proxy_config
import time, threading, random
from datetime import datetime, timezone, timedelta


class ProxyPool:
    HOMEPAGE_CIRCUIT_BREAKER_THRESHOLD = 3
    HOMEPAGE_CIRCUIT_BREAKER_SECONDS = 15 * 60
    FAILURE_COOLDOWN_SECONDS = 5 * 60

    def __init__(self):
        self._index = 0
        self._lock = threading.Lock()

    @staticmethod
    def _is_cooldown_enabled() -> bool:
        try:
            value = str(config_store.get("proxy_pool_cooldown_enabled", "true") or "").strip().lower()
        except Exception:
            value = "true"
        return value not in {"0", "false", "no", "off"}

    def get_next(self, region: str = "") -> Optional[str]:
        """加权轮询取一个可用代理，在高成功率代理间轮换"""
        now = datetime.now(timezone.utc)
        with Session(engine) as s:
            q = select(ProxyModel).where(ProxyModel.is_active == True)
            if region:
                q = q.where(ProxyModel.region == region)
            proxies = [
                p
                for p in s.exec(q).all()
                if (
                    not self._is_cooldown_enabled()
                    or getattr(p, "homepage_circuit_open_until", None) is None
                    or p.homepage_circuit_open_until <= now
                )
            ]
            if not proxies:
                return None
            proxies.sort(
                key=lambda p: (
                    p.homepage_success_count / max(p.homepage_success_count + p.homepage_fail_count, 1),
                    p.success_count / max(p.success_count + p.fail_count, 1),
                ),
                reverse=True,
            )
            with self._lock:
                idx = self._index % len(proxies)
                self._index += 1
            return proxies[idx].url

    def report_success(self, url: str) -> None:
        with Session(engine) as s:
            p = s.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if p:
                p.success_count += 1
                p.last_checked = datetime.now(timezone.utc)
                s.add(p)
                s.commit()

    def report_fail(self, url: str) -> None:
        with Session(engine) as s:
            p = s.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if p:
                p.fail_count += 1
                now = datetime.now(timezone.utc)
                p.last_checked = now
                if self._is_cooldown_enabled():
                    p.homepage_circuit_open_until = now + timedelta(seconds=self.FAILURE_COOLDOWN_SECONDS)
                s.add(p)
                s.commit()

    def report_homepage_success(self, url: str, *, status_code: int = 200) -> None:
        with Session(engine) as s:
            p = s.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if not p:
                return
            now = datetime.now(timezone.utc)
            p.homepage_success_count = int(getattr(p, "homepage_success_count", 0) or 0) + 1
            p.homepage_consecutive_failures = 0
            p.homepage_last_error = ""
            p.homepage_last_status_code = int(status_code or 200)
            p.homepage_last_checked = now
            if self._is_cooldown_enabled():
                p.homepage_circuit_open_until = None
            p.last_checked = now
            s.add(p)
            s.commit()

    def report_homepage_fail(
        self,
        url: str,
        *,
        error_message: str = "",
        status_code: int = 0,
    ) -> None:
        with Session(engine) as s:
            p = s.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if not p:
                return
            now = datetime.now(timezone.utc)
            consecutive = int(getattr(p, "homepage_consecutive_failures", 0) or 0) + 1
            p.homepage_fail_count = int(getattr(p, "homepage_fail_count", 0) or 0) + 1
            p.homepage_consecutive_failures = consecutive
            p.homepage_last_error = str(error_message or "")[:500]
            p.homepage_last_status_code = int(status_code or 0)
            p.homepage_last_checked = now
            p.last_checked = now
            if self._is_cooldown_enabled() and consecutive >= self.HOMEPAGE_CIRCUIT_BREAKER_THRESHOLD:
                p.homepage_circuit_open_until = now + timedelta(seconds=self.HOMEPAGE_CIRCUIT_BREAKER_SECONDS)
            s.add(p)
            s.commit()

    def check_all(self) -> dict:
        """检测所有代理可用性"""
        import requests

        with Session(engine) as s:
            proxies = s.exec(select(ProxyModel)).all()
        results = {"ok": 0, "fail": 0}
        for p in proxies:
            try:
                r = requests.get(
                    "https://httpbin.org/ip",
                    proxies=build_requests_proxy_config(p.url),
                    timeout=8,
                )
                if r.status_code == 200:
                    self.report_success(p.url)
                    results["ok"] += 1
                    continue
            except Exception:
                pass
            self.report_fail(p.url)
            results["fail"] += 1
        return results


proxy_pool = ProxyPool()
