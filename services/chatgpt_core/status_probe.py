"""ChatGPT 本地真实状态探测。"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from curl_cffi import requests as cffi_requests
from services.chatgpt_account_state import is_account_deactivated_message
from .token_refresh import TokenRefreshManager

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CHATGPT_ME_URL = "https://chatgpt.com/backend-api/me"
CHATGPT_ACCOUNTS_CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
CODEX_USER_AGENT = "codex_cli_rs/0.116.0 (Mac OS 26.0.1; arm64) Apple_Terminal/464"


def _probe_timeout_seconds() -> float:
    raw = str(os.getenv("CHATGPT_STATUS_PROBE_TIMEOUT_SECONDS") or "8").strip()
    try:
        return max(float(raw), 1.0)
    except Exception:
        return 8.0


STATUS_PROBE_TIMEOUT_SECONDS = _probe_timeout_seconds()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_proxies(proxy: Optional[str]) -> Optional[dict[str, str]]:
    if proxy:
        return {"http": proxy, "https": proxy}
    return None


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_auth_info(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("https://api.openai.com/auth", {})
    if isinstance(nested, dict):
        return nested
    return {}


def extract_chatgpt_account_id(account: Any) -> str:
    user_id = str(getattr(account, "user_id", "") or "").strip()
    if user_id:
        return user_id

    extra = getattr(account, "extra", {}) or {}
    id_token = str(extra.get("id_token") or getattr(account, "id_token", "") or "").strip()
    access_token = str(
        extra.get("access_token")
        or getattr(account, "access_token", "")
        or getattr(account, "token", "")
        or ""
    ).strip()

    id_payload = _decode_jwt_payload(id_token)
    auth_info = _extract_auth_info(id_payload)
    account_id = str(auth_info.get("chatgpt_account_id") or auth_info.get("account_id") or "").strip()
    if account_id:
        return account_id

    access_payload = _decode_jwt_payload(access_token)
    auth_info = _extract_auth_info(access_payload)
    return str(auth_info.get("chatgpt_account_id") or auth_info.get("account_id") or "").strip()


def extract_chatgpt_organization_id(account: Any, access_token: str = "") -> str:
    extra = getattr(account, "extra", {}) or {}
    direct = str(extra.get("organization_id") or getattr(account, "organization_id", "") or "").strip()
    if direct:
        return direct

    id_token = str(extra.get("id_token") or getattr(account, "id_token", "") or "").strip()
    for token in (id_token, access_token):
        payload = _decode_jwt_payload(token)
        auth_info = _extract_auth_info(payload)
        organization_id = str(
            auth_info.get("organization_id")
            or auth_info.get("poid")
            or ""
        ).strip()
        if organization_id:
            return organization_id
        organizations = auth_info.get("organizations") or []
        if isinstance(organizations, list):
            for item in organizations:
                if isinstance(item, dict):
                    organization_id = str(item.get("id") or "").strip()
                    if organization_id:
                        return organization_id
    return ""


def _parse_loose_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_header_error_json(headers: Any) -> dict[str, Any]:
    if not headers:
        return {}
    raw = headers.get("X-Error-Json") or headers.get("x-error-json") or ""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    raw = str(raw or "").strip()
    if not raw:
        return {}
    try:
        decoded = base64.b64decode(raw).decode("utf-8", errors="ignore")
    except Exception:
        return {}
    return _parse_loose_json(decoded)


def _extract_error_code(headers: Any, body_json: dict[str, Any], header_error_json: dict[str, Any]) -> str:
    for key in ("X-Openai-Ide-Error-Code", "x-openai-ide-error-code"):
        value = headers.get(key) if headers else None
        if isinstance(value, list):
            value = value[0] if value else ""
        if str(value or "").strip():
            return str(value).strip()

    candidates = [
        ((body_json.get("error") or {}).get("code") if isinstance(body_json.get("error"), dict) else ""),
        ((header_error_json.get("error") or {}).get("code") if isinstance(header_error_json.get("error"), dict) else ""),
    ]
    for candidate in candidates:
        if str(candidate or "").strip():
            return str(candidate).strip()
    return ""


def _extract_error_message(body_json: dict[str, Any], header_error_json: dict[str, Any], body_text: str, status_code: int) -> str:
    candidates = [
        ((body_json.get("error") or {}).get("message") if isinstance(body_json.get("error"), dict) else ""),
        ((header_error_json.get("error") or {}).get("message") if isinstance(header_error_json.get("error"), dict) else ""),
        body_json.get("message", ""),
        body_text.strip(),
    ]
    for candidate in candidates:
        if str(candidate or "").strip():
            return str(candidate).strip()[:500]
    return f"HTTP {status_code}"


@dataclass
class ProbeHTTPResult:
    status_code: int
    headers: Any
    body_text: str
    body_json: dict[str, Any]
    error_code: str
    message: str


def _probe_exception_message(exc: Exception) -> str:
    message = str(exc or "").strip()
    if not message:
        message = exc.__class__.__name__
    return message[:500]


def _failed_probe_result(exc: Exception) -> ProbeHTTPResult:
    return ProbeHTTPResult(
        status_code=0,
        headers={},
        body_text="",
        body_json={},
        error_code="",
        message=_probe_exception_message(exc),
    )


def _perform_get(url: str, headers: dict[str, str], proxy: Optional[str]) -> ProbeHTTPResult:
    try:
        response = cffi_requests.get(
            url,
            headers=headers,
            proxies=_build_proxies(proxy),
            timeout=STATUS_PROBE_TIMEOUT_SECONDS,
            impersonate="chrome110",
        )
    except Exception as exc:
        return _failed_probe_result(exc)
    body_text = response.text or ""
    body_json = _parse_loose_json(body_text)
    header_error_json = _parse_header_error_json(response.headers)
    error_code = _extract_error_code(response.headers, body_json, header_error_json)
    message = _extract_error_message(body_json, header_error_json, body_text, response.status_code)
    return ProbeHTTPResult(
        status_code=response.status_code,
        headers=response.headers,
        body_text=body_text,
        body_json=body_json,
        error_code=error_code,
        message=message,
    )


def _normalize_plan_type(plan_type: str, workspace_plan_type: str) -> str:
    raw = f"{plan_type} {workspace_plan_type}".strip().lower()
    if not raw:
        return "unknown"
    if "enterprise" in raw:
        return "enterprise"
    if "team" in raw:
        return "team"
    if "plus" in raw:
        return "plus"
    if "pro" in raw:
        return "pro"
    if "free" in raw:
        return "free"
    return plan_type.strip().lower() or workspace_plan_type.strip().lower() or "unknown"


def _probe_backend_me(access_token: str, proxy: Optional[str]) -> ProbeHTTPResult:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": CODEX_USER_AGENT,
    }
    return _perform_get(CHATGPT_ME_URL, headers=headers, proxy=proxy)


def _probe_accounts_check(access_token: str, proxy: Optional[str]) -> ProbeHTTPResult:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": CODEX_USER_AGENT,
    }
    return _perform_get(CHATGPT_ACCOUNTS_CHECK_URL, headers=headers, proxy=proxy)


def _probe_codex_usage(access_token: str, account_id: str, proxy: Optional[str]) -> ProbeHTTPResult:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": CODEX_USER_AGENT,
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    return _perform_get(CODEX_USAGE_URL, headers=headers, proxy=proxy)


def _extract_accounts_check_plan_type(acct: dict[str, Any]) -> str:
    account = acct.get("account") if isinstance(acct.get("account"), dict) else {}
    plan_type = str(account.get("plan_type") or "").strip()
    if plan_type:
        return plan_type
    entitlement = acct.get("entitlement") if isinstance(acct.get("entitlement"), dict) else {}
    return str(entitlement.get("subscription_plan") or "").strip()


def _extract_accounts_check_expires_at(acct: dict[str, Any]) -> str:
    entitlement = acct.get("entitlement") if isinstance(acct.get("entitlement"), dict) else {}
    return str(entitlement.get("expires_at") or "").strip()


def _select_accounts_check_subscription(
    body: dict[str, Any],
    preferred_keys: list[str] | tuple[str, ...],
    scope_hint: str = "",
) -> tuple[str, str]:
    accounts = body.get("accounts") if isinstance(body.get("accounts"), dict) else {}
    if not accounts:
        return "", ""

    seen_keys: set[str] = set()
    for raw_key in preferred_keys or []:
        key = str(raw_key or "").strip()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        acct = accounts.get(key)
        if isinstance(acct, dict):
            plan_type = _extract_accounts_check_plan_type(acct)
            expires_at = _extract_accounts_check_expires_at(acct)
            if plan_type:
                return plan_type, expires_at

    default_candidate = ("", "")
    paid_candidate = ("", "")
    free_candidate = ("", "")
    any_candidate = ("", "")
    for acct in accounts.values():
        if not isinstance(acct, dict):
            continue
        plan_type = _extract_accounts_check_plan_type(acct)
        if not plan_type:
            continue
        expires_at = _extract_accounts_check_expires_at(acct)
        normalized_plan = _normalize_plan_type(plan_type, "")
        if not any_candidate[0]:
            any_candidate = (plan_type, expires_at)
        account = acct.get("account") if isinstance(acct.get("account"), dict) else {}
        if bool(account.get("is_default")) and not default_candidate[0]:
            default_candidate = (plan_type, expires_at)
        if normalized_plan == "free" and not free_candidate[0]:
            free_candidate = (plan_type, expires_at)
        if normalized_plan != "free" and normalized_plan != "unknown" and not paid_candidate[0]:
            paid_candidate = (plan_type, expires_at)

    normalized_scope = str(scope_hint or "").strip().lower()
    if normalized_scope == "free":
        if default_candidate[0]:
            return default_candidate
        if free_candidate[0]:
            return free_candidate
        return any_candidate
    if normalized_scope == "business":
        if paid_candidate[0]:
            return paid_candidate
        return any_candidate

    if default_candidate[0]:
        return default_candidate
    if paid_candidate[0]:
        return paid_candidate
    if free_candidate[0]:
        return free_candidate
    return any_candidate


def _auth_state_for_source(source: str, *, valid: bool = False, invalidated: bool = False) -> str:
    source = str(source or "refresh_token").strip().lower() or "refresh_token"
    if valid:
        return "access_token_valid" if source == "access_token" else "refresh_token_valid"
    if invalidated:
        return "access_token_invalidated" if source == "access_token" else "refresh_token_invalidated"
    return "unauthorized"


def _resolve_probe_access_token(
    *,
    refresh_token: str,
    access_token: str,
    client_id: str,
    proxy: Optional[str],
) -> dict[str, Any]:
    manager = TokenRefreshManager(proxy_url=proxy)
    refresh_token = str(refresh_token or "").strip()
    access_token = str(access_token or "").strip()

    refresh_error_message = ""
    refresh_http_status = 0
    refresh_error_code = ""

    if refresh_token:
        refresh_result = manager.refresh_by_oauth_token(refresh_token=refresh_token, client_id=client_id or None)
        if refresh_result.success and str(refresh_result.access_token or "").strip():
            return {
                "ok": True,
                "source": "refresh_token",
                "access_token": str(refresh_result.access_token or "").strip(),
                "refresh_token": str(refresh_result.refresh_token or refresh_token or "").strip(),
                "http_status": 200,
                "error_code": "",
                "message": "refresh_token 刷新成功",
            }

        refresh_error_message = str(refresh_result.error_message or "refresh_token 刷新失败").strip()
        refresh_http_status = 401 if "HTTP 401" in refresh_error_message else 403 if "HTTP 403" in refresh_error_message else 0
        refresh_error_code = "token_invalidated" if refresh_http_status == 401 else ""

        if not access_token:
            return {
                "ok": False,
                "source": "refresh_token",
                "access_token": "",
                "refresh_token": refresh_token,
                "http_status": refresh_http_status,
                "error_code": refresh_error_code,
                "message": refresh_error_message,
            }

    if access_token:
        fallback_message = (
            f"{refresh_error_message}；回退使用现有 access_token 继续探测"
            if refresh_error_message
            else "账号缺少 refresh_token；使用现有 access_token 继续探测"
        )
        return {
            "ok": True,
            "source": "access_token",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "http_status": 200,
            "error_code": "",
            "message": fallback_message,
        }

    return {
        "ok": False,
        "source": "refresh_token",
        "access_token": "",
        "refresh_token": "",
        "http_status": 0,
        "error_code": "",
        "message": "账号缺少 refresh_token 且没有可用 access_token",
    }


def probe_local_chatgpt_status(account: Any, proxy: Optional[str] = None) -> dict[str, Any]:
    checked_at = _utcnow_iso()
    extra = getattr(account, "extra", {}) or {}
    refresh_token = str(extra.get("refresh_token") or getattr(account, "refresh_token", "") or "").strip()
    access_token = str(
        extra.get("access_token")
        or getattr(account, "access_token", "")
        or getattr(account, "token", "")
        or ""
    ).strip()
    client_id = str(extra.get("client_id") or getattr(account, "client_id", "") or "").strip()
    account_id = extract_chatgpt_account_id(account)
    organization_id = extract_chatgpt_organization_id(account)
    workspace_id = str(extra.get("workspace_id") or getattr(account, "workspace_id", "") or "").strip()
    workspace_scope = str(extra.get("chatgpt_workspace_scope") or getattr(account, "workspace_scope", "") or "").strip()

    result: dict[str, Any] = {
        "version": 1,
        "checked_at": checked_at,
        "auth": {
            "state": "unknown",
            "checked_at": checked_at,
            "source": "unknown",
            "http_status": 0,
            "error_code": "",
            "message": "",
            "refresh_available": bool(refresh_token),
            "access_available": bool(access_token),
        },
        "subscription": {
            "plan": "unknown",
            "checked_at": checked_at,
            "source": "unknown",
            "workspace_plan_type": "",
            "subscription_active_until": "",
            "chatgpt_account_id": account_id,
        },
        "codex": {
            "state": "not_checked",
            "checked_at": checked_at,
            "source": "unknown",
            "http_status": 0,
            "error_code": "",
            "message": "",
            "chatgpt_account_id": account_id,
        },
    }

    token_resolution = _resolve_probe_access_token(
        refresh_token=refresh_token,
        access_token=access_token,
        client_id=client_id,
        proxy=proxy,
    )
    token_source = str(token_resolution.get("source") or "refresh_token").strip() or "refresh_token"
    result["auth"]["source"] = token_source
    result["subscription"]["source"] = token_source
    result["codex"]["source"] = token_source

    if not token_resolution.get("ok"):
        http_status = int(token_resolution.get("http_status") or 0)
        error_code = str(token_resolution.get("error_code") or "").strip()
        message = str(token_resolution.get("message") or "").strip()
        if http_status == 401:
            state = _auth_state_for_source(token_source, invalidated=True)
        elif http_status == 403:
            state = "account_deactivated" if is_account_deactivated_message(error_code, message) else "banned_like"
        elif http_status == 0 and not refresh_token and not access_token:
            state = "missing_refresh_token"
        else:
            state = "probe_failed"
        result["auth"].update(
            {
                "state": state,
                "http_status": http_status,
                "error_code": error_code,
                "message": message,
            }
        )
        if state == "probe_failed":
            result["codex"].update(
                {
                    "state": "not_checked",
                    "message": f"本地 {token_source} 探测失败，未执行 Codex 探测",
                }
            )
        else:
            result["codex"].update(
                {
                    "state": "skipped_auth_invalid",
                    "message": f"本地 {token_source} 未通过校验，跳过 Codex 探测",
                }
            )
        return result

    probe_access_token = str(token_resolution.get("access_token") or "").strip()
    try:
        me_result = _probe_backend_me(probe_access_token, proxy=proxy)
    except Exception as exc:
        me_result = _failed_probe_result(exc)
    result["auth"].update(
        {
            "http_status": me_result.status_code,
            "error_code": me_result.error_code,
            "message": me_result.message,
        }
    )

    if me_result.status_code == 200:
        body = me_result.body_json if isinstance(me_result.body_json, dict) else {}
        plan_type = str(body.get("plan_type") or "").strip()
        workspace_plan_type = ""
        orgs = ((body.get("orgs") or {}).get("data") if isinstance(body.get("orgs"), dict) else []) or []
        if isinstance(orgs, list):
            for org in orgs:
                if not isinstance(org, dict):
                    continue
                settings = org.get("settings") or {}
                if isinstance(settings, dict) and str(settings.get("workspace_plan_type") or "").strip():
                    workspace_plan_type = str(settings.get("workspace_plan_type") or "").strip()
                    break

        result["auth"]["state"] = _auth_state_for_source(token_source, valid=True)
        normalized_plan = _normalize_plan_type(plan_type, workspace_plan_type)
        subscription_active_until = str(
            body.get("chatgpt_subscription_active_until")
            or body.get("subscription_active_until")
            or ""
        ).strip()

        if normalized_plan == "unknown" or not subscription_active_until:
            try:
                accounts_check = _probe_accounts_check(probe_access_token, proxy=proxy)
            except Exception as exc:
                accounts_check = _failed_probe_result(exc)
            if accounts_check.status_code == 200 and accounts_check.body_json:
                matched_org_id = organization_id or extract_chatgpt_organization_id(account, probe_access_token)
                preferred_keys = [
                    workspace_id,
                    account_id,
                    matched_org_id,
                ]
                plan_type_from_accounts, expires_at_from_accounts = _select_accounts_check_subscription(
                    accounts_check.body_json,
                    preferred_keys,
                    workspace_scope,
                )
                if plan_type_from_accounts:
                    accounts_plan = _normalize_plan_type(plan_type_from_accounts, workspace_plan_type)
                    if normalized_plan == "unknown":
                        normalized_plan = accounts_plan
                    if expires_at_from_accounts:
                        subscription_active_until = expires_at_from_accounts
                    result["subscription"]["source"] = "accounts_check"

        result["subscription"].update(
            {
                "plan": normalized_plan,
                "workspace_plan_type": workspace_plan_type,
                "subscription_active_until": subscription_active_until,
            }
        )

        if not account_id:
            result["codex"].update(
                {
                    "state": "probe_failed",
                    "message": "缺少 Chatgpt-Account-Id，无法严格探测 Codex 状态",
                }
            )
            return result

        try:
            codex_result = _probe_codex_usage(probe_access_token, account_id=account_id, proxy=proxy)
        except Exception as exc:
            codex_result = _failed_probe_result(exc)
        result["codex"].update(
            {
                "http_status": codex_result.status_code,
                "error_code": codex_result.error_code,
                "message": codex_result.message,
            }
        )
        try:
            from services.chatgpt_core.codex_usage import (
                build_codex_usage_extra_updates,
                parse_codex_rate_limit_headers,
                parse_codex_usage_body,
            )

            snapshot = parse_codex_rate_limit_headers(codex_result.headers, updated_at=checked_at)
            if not snapshot:
                snapshot = parse_codex_usage_body(codex_result.body_json, updated_at=checked_at)
            usage = build_codex_usage_extra_updates(snapshot, checked_at) if snapshot else {}
            if usage:
                result["codex"]["usage"] = usage
        except Exception:
            pass
        if codex_result.status_code == 200:
            result["codex"]["state"] = "usable"
        elif codex_result.status_code == 401:
            if codex_result.error_code == "token_invalidated":
                result["codex"]["state"] = _auth_state_for_source(token_source, invalidated=True)
            else:
                result["codex"]["state"] = "unauthorized"
        elif is_account_deactivated_message(codex_result.error_code, codex_result.message):
            result["codex"]["state"] = "account_deactivated"
        elif codex_result.status_code in (402, 403):
            result["codex"]["state"] = "payment_required"
        elif codex_result.status_code == 429:
            result["codex"]["state"] = "quota_exhausted"
        else:
            result["codex"]["state"] = "probe_failed"
        return result

    if me_result.status_code == 401:
        result["auth"]["state"] = _auth_state_for_source(
            token_source,
            invalidated=True,
        )
        result["codex"].update(
            {
                "state": "skipped_auth_invalid",
                "message": f"本地 {token_source} 未通过 /backend-api/me 校验，跳过 Codex 探测",
            }
        )
        return result

    if me_result.status_code in (402, 403):
        if is_account_deactivated_message(me_result.error_code, me_result.message):
            result["auth"]["state"] = "account_deactivated"
        else:
            result["auth"]["state"] = "banned_like" if me_result.status_code == 403 else "probe_failed"
        result["codex"].update(
            {
                "state": "skipped_auth_invalid",
                "message": f"本地 {token_source} 被拒绝，跳过 Codex 探测",
            }
        )
        return result

    result["auth"]["state"] = "probe_failed"
    result["codex"].update(
        {
            "state": "not_checked",
            "message": "本地认证探测失败，未执行 Codex 探测",
        }
    )
    return result
