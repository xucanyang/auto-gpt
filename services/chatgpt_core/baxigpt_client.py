"""BaxiGPT 卡密提交接口客户端 (Modified to use openai-pay-submit backend)."""
from __future__ import annotations

import os
import time
from typing import Any

from curl_cffi import requests as cffi_requests

# Pointing to openai-pay-submit local instance
DEFAULT_BASE_URL = os.environ.get("OAIPAY_SUBMIT_URL", "http://openai-pay-submit:8789")
DEFAULT_WORKER_TOKEN = os.environ.get("OAIPAY_WORKER_TOKEN", "xucanyang")


class BaxiGptRequestError(RuntimeError):
    pass


class BaxiGptClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 30,
        submit_timeout: int = 120,
        retries: int = 2,
        submit_retries: int = 0,
        retry_backoff_seconds: float = 0.8,
    ):
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = DEFAULT_WORKER_TOKEN
        self.timeout = max(int(timeout or 30), 1)
        self.submit_timeout = max(int(submit_timeout or 120), self.timeout)
        self.retries = max(int(retries or 0), 0)
        self.submit_retries = max(int(submit_retries or 0), 0)
        self.retry_backoff_seconds = max(float(retry_backoff_seconds or 0), 0.0)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}"
        }

    def _sleep_before_retry(self, attempt_index: int) -> None:
        if self.retry_backoff_seconds <= 0:
            return
        delay = min(self.retry_backoff_seconds * (2 ** max(attempt_index - 1, 0)), 8.0)
        if delay > 0:
            time.sleep(delay)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        *,
        timeout: int | None = None,
        retries: int | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        max_retries = self.retries if retries is None else max(int(retries or 0), 0)
        attempts = max_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = cffi_requests.request(
                    method,
                    url,
                    json=payload,
                    params=params,
                    headers=self._headers(),
                    timeout=max(int(timeout or self.timeout), 1),
                    impersonate="chrome",
                )
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise BaxiGptRequestError(f"请求上游失败: {exc}") from exc

            if response.status_code >= 400:
                text = str(getattr(response, "text", "") or "")[:500]
                error = BaxiGptRequestError(f"上游 HTTP {response.status_code}: {text}")
                last_error = error
                if attempt < attempts and response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                    self._sleep_before_retry(attempt)
                    continue
                raise error
            
            try:
                data = response.json()
            except Exception as exc:
                text = str(getattr(response, "text", "") or "")[:500]
                last_error = BaxiGptRequestError(f"上游响应不是 JSON: {text}")
                if attempt < attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise BaxiGptRequestError(f"上游响应不是 JSON: {text}") from exc
            
            if not isinstance(data, dict):
                last_error = BaxiGptRequestError("上游响应不是对象")
                if attempt < attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise last_error
            return data
        
        if last_error is not None:
            raise BaxiGptRequestError(str(last_error)) from last_error
        raise BaxiGptRequestError("请求上游失败")

    def code_info(self, code: str) -> dict[str, Any]:
        res = self._request("GET", "/api/task/cdk/check", params={"cdk": str(code or "")})
        return {
            "ok": True,
            "remaining": res.get("balance", 0),
            "total": res.get("balance", 0),
        }

    def submit(self, *, code: str, access_token: str | list[str]) -> dict[str, Any]:
        accounts = [access_token] if isinstance(access_token, str) else [str(t or "") for t in access_token if str(t or "").strip()]
        payload = {
            "cdk": str(code or ""),
            "accounts": accounts,
            "account": accounts[0] if accounts else ""
        }
        res = self._request("POST", "/api/task/submit", payload=payload, timeout=self.submit_timeout, retries=self.submit_retries)
        
        status_res = self._request("GET", "/api/task/status", params={"cdk": str(code or "")})
        tasks = status_res.get("tasks", [])
        
        submitted_items = []
        for token in accounts:
            task_id = ""
            matched_t = None
            for t in tasks:
                t_acc = str(t.get("account") or "")
                if t_acc == token or (token and t_acc.startswith(token[:20])):
                    matched_t = t
                    task_id = str(t.get("id", ""))
                    break
            if not task_id:
                task_id = f"fallback_{token[:20]}"
            submitted_items.append({
                "account": token,
                "task_id": task_id,
                "order_id": f"{code}::{task_id}",
                "display_id": task_id,
                "status": "submitted",
                "raw_task": matched_t or {}
            })
            
        first_item = submitted_items[0] if submitted_items else {"order_id": f"{code}::fallback", "display_id": "fallback", "status": "submitted"}
        return {
            "ok": True,
            "order_id": first_item["order_id"],
            "display_id": first_item.get("display_id", ""),
            "status": "submitted",
            "submitted_items": submitted_items,
            "raw_response": res
        }

    def status(self, order_id: str) -> dict[str, Any]:
        if "::" not in order_id:
            return {"ok": False, "status": "failed", "message": f"Invalid order_id format: {order_id}"}
            
        code, task_id = order_id.split("::", 1)
        res = self._request("GET", "/api/task/status", params={"cdk": code})
        tasks = res.get("tasks", [])
        
        for t in tasks:
            if str(t.get("id", "")) == task_id or (task_id.startswith("fallback_") and str(t.get("account", "")).startswith(task_id[9:])):
                raw_status = str(t.get("status", ""))
                if raw_status == "SUCCESS":
                    mapped = "paid"
                elif raw_status == "FAILED":
                    mapped = "failed"
                else:
                    mapped = "processing"
                    
                return {
                    "ok": True,
                    "status": mapped,
                    "display_id": str(t.get("id", "")),
                    "email": str(t.get("account", "")),
                    "message": str(t.get("error") or t.get("message") or t.get("reason") or "")
                }
                
        return {"ok": False, "status": "processing", "message": "Task not found in status list yet"}

    def query(self, code: str) -> dict[str, Any]:
        res = self._request("GET", "/api/task/status", params={"cdk": str(code or "")})
        tasks = res.get("tasks", [])
        
        orders = []
        for t in tasks:
            raw_status = str(t.get("status", ""))
            if raw_status == "SUCCESS":
                mapped = "paid"
            elif raw_status == "FAILED":
                mapped = "failed"
            else:
                mapped = "processing"
                
            orders.append({
                "order_id": f"{code}::{t.get('id', '')}",
                "status": mapped,
                "display_id": str(t.get("id", "")),
                "email": str(t.get("account", "")),
                "message": str(t.get("error") or t.get("message") or t.get("reason") or "")
            })
            
        return {
            "ok": True,
            "orders": orders,
            "remaining": len(orders), 
            "total": len(orders)
        }
