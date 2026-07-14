"""BaxiGPT 卡密提交接口客户端 (Modified to use openai-pay-submit backend)."""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from curl_cffi import requests as cffi_requests

# Pointing to openai-pay-submit local instance
DEFAULT_BASE_URL = os.environ.get("OAIPAY_SUBMIT_URL", "http://openai-pay-submit:8789")
DEFAULT_WORKER_TOKEN = os.environ.get("OAIPAY_WORKER_TOKEN", "xucanyang")


class BaxiGptRequestError(RuntimeError):
    """Upstream request failure with submit-retry safety information."""

    def __init__(self, message: str, *, request_outcome_unknown: bool = False, http_status: int = 0):
        super().__init__(message)
        # A transport failure can happen after the upstream has accepted a PIX
        # task. Callers must not submit the same account/CDK pair again.
        self.request_outcome_unknown = bool(request_outcome_unknown)
        self.http_status = int(http_status or 0)


def _email_from_jwt(token: str) -> str:
    text = str(token or "").strip()
    parts = text.split(".")
    if len(parts) < 2:
        return ""
    try:
        payload_segment = parts[1]
        payload_segment += "=" * (-len(payload_segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_segment.encode("utf-8")).decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    profile = payload.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        email = str(profile.get("email") or "").strip()
        if email:
            return email
    for key in ("email", "preferred_username", "username"):
        email = str(payload.get(key) or "").strip()
        if email:
            return email
    return ""


def _looks_like_fallback_task_id(task_id: str) -> bool:
    value = str(task_id or "").strip().lower()
    return not value or value.startswith("fallback_")


def _task_message(task: dict[str, Any] | None, fallback: str = "") -> str:
    data = task if isinstance(task, dict) else {}
    for key in ("fail_reason", "raw_fail_reason", "error", "message", "msg", "reason", "detail"):
        text = str(data.get(key) or "").strip()
        if text:
            return text
    return fallback


def _normalize_task_status(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"success", "paid", "completed"}:
        return "paid"
    if raw in {"failed", "fail", "expired", "cancelled", "canceled", "invalid", "error", "used"}:
        return "failed"
    if raw in {"pending", "processing", "submitted", "extracting", "wait_scan", "verifying", "wait-scan"}:
        return "processing"
    return raw or "processing"


def _redact_sensitive_text(value: Any, *secrets: Any) -> str:
    text = str(value or "")
    for secret in secrets:
        raw = str(secret or "").strip()
        if raw:
            text = text.replace(raw, "[REDACTED]")
    return text


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
                raise BaxiGptRequestError(
                    f"请求上游失败: {exc}",
                    request_outcome_unknown=True,
                ) from exc

            if response.status_code >= 400:
                text = str(getattr(response, "text", "") or "")[:500]
                error = BaxiGptRequestError(
                    f"上游 HTTP {response.status_code}: {text}",
                    http_status=int(response.status_code or 0),
                )
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
                raise BaxiGptRequestError(
                    f"上游响应不是 JSON: {text}",
                    request_outcome_unknown=True,
                ) from exc
            
            if not isinstance(data, dict):
                last_error = BaxiGptRequestError("上游响应不是对象")
                if attempt < attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise BaxiGptRequestError(
                    str(last_error),
                    request_outcome_unknown=True,
                )
            return data
        
        if last_error is not None:
            raise BaxiGptRequestError(
                str(last_error),
                request_outcome_unknown=bool(getattr(last_error, "request_outcome_unknown", False)),
            ) from last_error
        raise BaxiGptRequestError("请求上游失败")

    def code_info(self, code: str) -> dict[str, Any]:
        res = self._request("GET", "/api/task/cdk/check", params={"cdk": str(code or "")})
        balance = int(res.get("balance") if "balance" in res else res.get("remaining") or 0)
        total = int(res.get("initial_balance") or res.get("total") or balance or 0)
        blocked_reason = str(res.get("blocked_reason") or "").strip()
        can_submit_raw = res.get("can_submit")
        can_submit = bool(can_submit_raw) if "can_submit" in res else balance > 0
        ok = can_submit and not blocked_reason and balance > 0
        return {
            "ok": ok,
            "remaining": balance,
            "total": total,
            "failed_count": int(res.get("failed_count") or 0),
            "can_submit": can_submit,
            "blocked_reason": blocked_reason,
            "message": blocked_reason or ("" if ok else "卡密不可提交"),
            "raw_response": res,
        }

    def submit(self, *, code: str, access_token: str | list[str]) -> dict[str, Any]:
        accounts = [access_token] if isinstance(access_token, str) else [str(t or "") for t in access_token if str(t or "").strip()]
        payload = {
            "cdk": str(code or ""),
            "accounts": accounts,
            "account": accounts[0] if accounts else ""
        }
        res = self._request("POST", "/api/task/submit", payload=payload, timeout=self.submit_timeout, retries=self.submit_retries)
        created_tasks = [
            item for item in (
                res.get("created_tasks")
                if isinstance(res.get("created_tasks"), list)
                else res.get("tasks")
                if isinstance(res.get("tasks"), list)
                else []
            )
            if isinstance(item, dict)
        ]
        legacy_status_tasks: list[dict[str, Any]] = []
        if not created_tasks:
            status_res = self._request("GET", "/api/task/status", params={"cdk": str(code or "")})
            legacy_status_tasks = [item for item in (status_res.get("tasks") if isinstance(status_res.get("tasks"), list) else []) if isinstance(item, dict)]
        
        submitted_items = []
        unresolved: list[str] = []
        consumed_task_ids: set[str] = set()
        for token in accounts:
            token_email = _email_from_jwt(token)
            task_id = ""
            matched_t = None
            candidate_tasks = created_tasks or legacy_status_tasks
            for t in candidate_tasks:
                candidate_id = str(t.get("task_id") or t.get("id") or "").strip()
                if candidate_id and candidate_id in consumed_task_ids:
                    continue
                t_email = str(t.get("email") or t.get("account") or "").strip().lower()
                if token_email and t_email == token_email.lower():
                    matched_t = t
                    task_id = candidate_id
                    break
            if matched_t is None and len(candidate_tasks) == len(accounts):
                for t in candidate_tasks:
                    candidate_id = str(t.get("task_id") or t.get("id") or "").strip()
                    if candidate_id and candidate_id not in consumed_task_ids:
                        matched_t = t
                        task_id = candidate_id
                        break
            if matched_t is None:
                for t in candidate_tasks:
                    candidate_id = str(t.get("task_id") or t.get("id") or "").strip()
                    t_acc = str(t.get("account") or "")
                    if candidate_id and t_acc == token and candidate_id not in consumed_task_ids:
                        matched_t = t
                        task_id = candidate_id
                        break
            if _looks_like_fallback_task_id(task_id):
                unresolved.append(token_email or token[:20] or "unknown")
                continue
            consumed_task_ids.add(task_id)
            submitted_items.append({
                "account": token,
                "email": token_email or str((matched_t or {}).get("email") or (matched_t or {}).get("account") or ""),
                "task_id": task_id,
                "order_id": f"{code}::{task_id}",
                "display_id": task_id,
                "status": "submitted",
                "raw_task": matched_t or {}
            })
        if unresolved:
            return {
                "ok": False,
                "status": "unresolved",
                "message": "上游已受理但未返回可轮询任务ID: " + ", ".join(unresolved[:5]),
                "submitted_items": submitted_items,
                "raw_response": res,
            }
            
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
        if _looks_like_fallback_task_id(task_id):
            return {"ok": False, "status": "failed", "message": f"上游任务ID未解析，不能轮询: {task_id}"}
        try:
            res = self._request("GET", "/api/task/status", params={"task_id": task_id})
        except Exception:
            res = self._request("GET", "/api/task/status", params={"cdk": code})
        tasks = res.get("tasks", [])
        
        for t in tasks:
            if str(t.get("task_id") or t.get("id") or "") == task_id:
                mapped = _normalize_task_status(t.get("status"))
                    
                return {
                    "ok": True,
                    "status": mapped,
                    "display_id": str(t.get("task_id") or t.get("id") or ""),
                    "email": str(t.get("email") or t.get("account") or ""),
                    "message": _task_message(t),
                    "raw_task": t,
                }
                
        return {"ok": False, "status": "processing", "message": "Task not found in status list yet"}

    def submit_pix(self, *, pix_cdk: str, access_token: str) -> dict[str, Any]:
        """Create exactly one PIX auto-extract task.

        The PIX endpoint returns a one-time ``status_token``. It is deliberately
        returned only to the in-process caller; this client never emits a raw
        response that could accidentally persist the token or PIX CDK.
        """

        cdk = str(pix_cdk or "").strip()
        token = str(access_token or "").strip()
        if not cdk:
            return {"ok": False, "status": "failed", "message": "PIX CDK 不能为空"}
        if not token:
            return {"ok": False, "status": "failed", "message": "账号缺少 Access Token"}

        payload = {
            "submitMode": "pix_auto_extract",
            "pixCdk": cdk,
            "accounts": [token],
        }
        try:
            res = self._request(
                "POST",
                "/api/task/submit",
                payload=payload,
                timeout=self.submit_timeout,
                retries=self.submit_retries,
            )
        except BaxiGptRequestError as exc:
            raise BaxiGptRequestError(
                _redact_sensitive_text(exc, cdk),
                request_outcome_unknown=exc.request_outcome_unknown,
                http_status=exc.http_status,
            ) from exc

        created_tasks = [
            item
            for item in (
                res.get("created_tasks")
                if isinstance(res.get("created_tasks"), list)
                else res.get("tasks")
                if isinstance(res.get("tasks"), list)
                else []
            )
            if isinstance(item, dict)
        ]
        task = created_tasks[0] if created_tasks else {}
        task_id = str(task.get("task_id") or task.get("id") or "").strip()
        status_token = str(task.get("status_token") or task.get("statusToken") or "").strip()
        if not task_id or not status_token:
            # A response without the polling credential may still have created
            # the task. Never retry the same account/CDK automatically.
            return {
                "ok": False,
                "status": "unresolved",
                "message": "PIX 上游已响应但未返回可轮询任务凭据，请人工复核",
                "submission_unknown": True,
            }
        return {
            "ok": True,
            "status": _normalize_task_status(task.get("status")),
            "order_id": task_id,
            "display_id": task_id,
            "task_id": task_id,
            "status_token": status_token,
        }

    def pix_status(self, *, task_id: str, status_token: str) -> dict[str, Any]:
        """Poll a PIX task without exposing its one-time status credential."""

        task_id_value = str(task_id or "").strip()
        token = str(status_token or "").strip()
        if not task_id_value or not token:
            return {"ok": False, "status": "failed", "message": "PIX 轮询凭据不完整"}
        try:
            res = self._request(
                "GET",
                "/api/pix/tasks/status",
                params={"task_id": task_id_value, "status_token": token},
            )
        except BaxiGptRequestError as exc:
            raise BaxiGptRequestError(
                _redact_sensitive_text(exc, token),
                request_outcome_unknown=exc.request_outcome_unknown,
                http_status=exc.http_status,
            ) from exc

        tasks = res.get("tasks") if isinstance(res.get("tasks"), list) else []
        task = next(
            (
                item for item in tasks
                if isinstance(item, dict)
                and str(item.get("task_id") or item.get("id") or "").strip() == task_id_value
            ),
            None,
        )
        if not isinstance(task, dict):
            return {"ok": False, "status": "processing", "message": "PIX 任务暂未返回状态"}
        return {
            "ok": True,
            "status": _normalize_task_status(task.get("status")),
            "display_id": str(task.get("task_id") or task.get("id") or task_id_value),
            "email": str(task.get("email") or task.get("account") or ""),
            "message": _redact_sensitive_text(_task_message(task), token),
        }

    def query(self, code: str) -> dict[str, Any]:
        res = self._request("GET", "/api/task/status", params={"cdk": str(code or "")})
        tasks = res.get("tasks", [])
        try:
            info = self.code_info(code)
        except Exception as exc:
            info = {"ok": False, "remaining": 0, "total": 0, "message": str(exc)}
        
        orders = []
        for t in tasks:
            mapped = _normalize_task_status(t.get("status"))
                
            orders.append({
                "order_id": f"{code}::{t.get('task_id') or t.get('id', '')}",
                "status": mapped,
                "display_id": str(t.get("task_id") or t.get("id") or ""),
                "email": str(t.get("email") or t.get("account") or ""),
                "message": _task_message(t),
                "raw_task": t,
            })
            
        return {
            "ok": True,
            "orders": orders,
            "remaining": int(info.get("remaining") or 0),
            "total": int(info.get("total") or 0),
            "code_info": info,
        }
