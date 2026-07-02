"""代理池 - 从数据库读取代理，支持轮询、冷却和按出口特征选取"""

from typing import Optional, Any
import json
from sqlmodel import Session, select
from .db import ProxyModel, engine
from .config_store import config_store
from .proxy_utils import build_requests_proxy_config
import threading
from datetime import datetime, timezone, timedelta


class ProxyPool:
    HOMEPAGE_CIRCUIT_BREAKER_THRESHOLD = 3
    HOMEPAGE_CIRCUIT_BREAKER_SECONDS = 15 * 60
    FAILURE_COOLDOWN_SECONDS = 5 * 60

    def __init__(self):
        self._index = 0
        self._lock = threading.Lock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _normalize_dt(value):
        if value is None:
            return None
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @classmethod
    def _is_future(cls, value, now: datetime | None = None) -> bool:
        parsed = cls._normalize_dt(value)
        if parsed is None:
            return False
        return parsed > (now or cls._now())

    @staticmethod
    def _is_cooldown_enabled() -> bool:
        try:
            value = str(config_store.get("proxy_pool_cooldown_enabled", "true") or "").strip().lower()
        except Exception:
            value = "true"
        return value not in {"0", "false", "no", "off"}

    def _cooldown_open(self, proxy: ProxyModel, now: datetime | None = None) -> bool:
        if not self._is_cooldown_enabled():
            return False
        now = now or self._now()
        return self._is_future(getattr(proxy, "cooldown_until", None), now) or self._is_future(
            getattr(proxy, "homepage_circuit_open_until", None),
            now,
        )

    @staticmethod
    def _success_rate(success: int, fail: int) -> float:
        return float(success or 0) / max(int(success or 0) + int(fail or 0), 1)

    def _proxy_sort_key(self, proxy: ProxyModel) -> tuple[float, float, float, int]:
        return (
            float(getattr(proxy, "health_score", 0) or 0),
            self._success_rate(
                int(getattr(proxy, "homepage_success_count", 0) or 0),
                int(getattr(proxy, "homepage_fail_count", 0) or 0),
            ),
            self._success_rate(
                int(getattr(proxy, "success_count", 0) or 0),
                int(getattr(proxy, "fail_count", 0) or 0),
            ),
            -int(getattr(proxy, "consecutive_failures", 0) or 0),
        )

    @staticmethod
    def _last_probe(proxy: ProxyModel) -> dict[str, Any]:
        raw = str(getattr(proxy, "last_probe_json", "") or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _matches_target(self, proxy: ProxyModel, target: str) -> bool:
        target = str(target or "").strip().lower()
        if target in {"chatgpt", "homepage"}:
            status = str(getattr(proxy, "chatgpt_status", "") or "unchecked").strip().lower()
            if status == "ok":
                return True
            probe = self._last_probe(proxy)
            chatgpt_probe = probe.get("chatgpt") if isinstance(probe.get("chatgpt"), dict) else {}
            targets = chatgpt_probe.get("targets") if isinstance(chatgpt_probe.get("targets"), dict) else {}
            chatgpt_cffi = targets.get("chatgpt") if isinstance(targets.get("chatgpt"), dict) else {}
            auth_cffi = targets.get("auth") if isinstance(targets.get("auth"), dict) else {}
            # 兼容新扫描结果：注册链路要求 chatgpt.com 与 auth.openai.com 都通过
            # curl_cffi/chrome 指纹检测；普通 requests 403 只作为诊断，不再一票否决。
            if bool(chatgpt_cffi.get("ok")) and bool(auth_cffi.get("ok")):
                return True
            return status not in {
                "blocked_403",
                "rate_limited_429",
                "timeout",
                "tls_error",
                "dns_error",
                "proxy_error",
                "proxy_auth_failed",
                "connection_error",
                "connection_refused",
                "failed",
            }
        return True

    @staticmethod
    def _matches_country(proxy: ProxyModel, country_code: str) -> bool:
        expected = str(country_code or "").strip().upper()
        if not expected:
            return True
        actual = str(getattr(proxy, "exit_country_code", "") or "").strip().upper()
        if actual:
            return actual == expected
        region = str(getattr(proxy, "region", "") or "").strip().upper()
        desired = str(getattr(proxy, "desired_country_code", "") or "").strip().upper()
        return expected in {region, desired}

    def get_candidate_records(
        self,
        *,
        region: str = "",
        country_code: str = "",
        target: str = "",
        limit: int = 0,
        min_score: float = 0,
    ) -> list[dict[str, Any]]:
        """返回可用于任务执行的代理候选，包含评分和出口元数据。"""
        now = self._now()
        with Session(engine) as s:
            q = select(ProxyModel).where(ProxyModel.is_active == True)
            region_value = str(region or "").strip()
            if region_value:
                q = q.where(ProxyModel.region == region_value)
            rows = s.exec(q).all()
            proxies = []
            for proxy in rows:
                if self._cooldown_open(proxy, now):
                    continue
                if not self._matches_country(proxy, country_code):
                    continue
                if not self._matches_target(proxy, target):
                    continue
                score = float(getattr(proxy, "health_score", 0) or 0)
                if min_score and score < float(min_score):
                    continue
                proxies.append(proxy)
            proxies.sort(key=self._proxy_sort_key, reverse=True)
            if limit and limit > 0:
                proxies = proxies[: int(limit)]
            return [
                {
                    "id": int(proxy.id or 0),
                    "url": str(proxy.url or ""),
                    "region": str(proxy.region or ""),
                    "exit_country_code": str(getattr(proxy, "exit_country_code", "") or ""),
                    "exit_ip": str(getattr(proxy, "exit_ip", "") or ""),
                    "chatgpt_status": str(getattr(proxy, "chatgpt_status", "") or "unchecked"),
                    "health_score": float(getattr(proxy, "health_score", 0) or 0),
                    "latency_ms": int(getattr(proxy, "last_latency_ms", 0) or 0),
                    "source": "pool",
                }
                for proxy in proxies
                if str(proxy.url or "").strip()
            ]

    def get_next(self, region: str = "") -> Optional[str]:
        """加权轮询取一个可用代理，在高成功率代理间轮换"""
        proxies = self.get_candidate_records(region=region)
        if not proxies:
            return None
        with self._lock:
            idx = self._index % len(proxies)
            self._index += 1
        return str(proxies[idx].get("url") or "") or None

    def report_success(self, url: str) -> None:
        with Session(engine) as s:
            p = s.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if p:
                p.success_count += 1
                now = self._now()
                p.consecutive_failures = 0
                p.cooldown_until = None
                p.last_checked = now
                try:
                    from services.proxy_scanner import calculate_health_score

                    p.health_score = calculate_health_score(p)
                except Exception:
                    pass
                s.add(p)
                s.commit()

    def report_fail(self, url: str) -> None:
        with Session(engine) as s:
            p = s.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if p:
                p.fail_count += 1
                p.consecutive_failures = int(getattr(p, "consecutive_failures", 0) or 0) + 1
                now = self._now()
                p.last_checked = now
                if self._is_cooldown_enabled():
                    p.cooldown_until = now + timedelta(seconds=self.FAILURE_COOLDOWN_SECONDS)
                    p.homepage_circuit_open_until = p.cooldown_until
                try:
                    from services.proxy_scanner import calculate_health_score

                    p.health_score = calculate_health_score(p)
                except Exception:
                    pass
                s.add(p)
                s.commit()

    def report_homepage_success(self, url: str, *, status_code: int = 200) -> None:
        with Session(engine) as s:
            p = s.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if not p:
                return
            now = self._now()
            p.homepage_success_count = int(getattr(p, "homepage_success_count", 0) or 0) + 1
            p.homepage_consecutive_failures = 0
            p.homepage_last_error = ""
            p.homepage_last_status_code = int(status_code or 200)
            p.homepage_last_checked = now
            p.chatgpt_status = "ok"
            p.chatgpt_status_code = int(status_code or 200)
            p.chatgpt_last_error = ""
            p.chatgpt_last_checked_at = now
            if self._is_cooldown_enabled():
                p.homepage_circuit_open_until = None
            p.last_checked = now
            try:
                from services.proxy_scanner import calculate_health_score

                p.health_score = calculate_health_score(p)
            except Exception:
                pass
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
            now = self._now()
            consecutive = int(getattr(p, "homepage_consecutive_failures", 0) or 0) + 1
            p.homepage_fail_count = int(getattr(p, "homepage_fail_count", 0) or 0) + 1
            p.homepage_consecutive_failures = consecutive
            p.homepage_last_error = str(error_message or "")[:500]
            p.homepage_last_status_code = int(status_code or 0)
            p.homepage_last_checked = now
            p.chatgpt_status_code = int(status_code or 0)
            if int(status_code or 0) == 403:
                p.chatgpt_status = "blocked_403"
            elif int(status_code or 0) == 429:
                p.chatgpt_status = "rate_limited_429"
            elif int(status_code or 0) >= 400:
                p.chatgpt_status = f"http_{int(status_code or 0)}"
            elif error_message:
                p.chatgpt_status = "failed"
            p.chatgpt_last_error = str(error_message or "")[:500]
            p.chatgpt_last_checked_at = now
            p.last_checked = now
            if self._is_cooldown_enabled() and consecutive >= self.HOMEPAGE_CIRCUIT_BREAKER_THRESHOLD:
                p.homepage_circuit_open_until = now + timedelta(seconds=self.HOMEPAGE_CIRCUIT_BREAKER_SECONDS)
            try:
                from services.proxy_scanner import calculate_health_score

                p.health_score = calculate_health_score(p)
            except Exception:
                pass
            s.add(p)
            s.commit()

    def check_all(self) -> dict:
        """检测所有代理可用性"""
        from services.proxy_scanner import scan_proxy_id

        with Session(engine) as s:
            proxies = s.exec(select(ProxyModel)).all()
        results = {"ok": 0, "fail": 0, "degraded": 0}
        for p in proxies:
            result = scan_proxy_id(int(p.id or 0), targets=["basic", "geo", "chatgpt"], timeout_seconds=8)
            status = str(result.get("status") or "")
            if status == "ok":
                results["ok"] += 1
            elif status == "degraded":
                results["degraded"] += 1
            else:
                results["fail"] += 1
        return results


proxy_pool = ProxyPool()
