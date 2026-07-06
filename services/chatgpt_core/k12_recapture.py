"""Manual K12/workspace recapture for saved ChatGPT accounts.

This module reuses the registration-time K12 workspace capture primitives, but starts
from already persisted Web session material (access_token + cookies/session_token).
The persistence path is deliberately explicit instead of going through
``save_account`` wholesale, because saved free rows may already have refresh_token
or payment state that must not be downgraded by AT-only workspace artifacts.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlmodel import Session, select

from core.db import AccountModel
from services.account_filters import upsert_account_list_state_for_account_ids
from services.chatgpt_account_state import classify_chatgpt_capabilities
from services.chatgpt_core.chatgpt_client import ChatGPTClient
from services.chatgpt_core.k12_workspace import capture_k12_and_all_spaces, parse_k12_workspace_ids, safe_k12_error
from services.chatgpt_core.utils import coerce_browser_fingerprint

AUTH_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "__Secure-authjs.session-token",
    "next-auth.session-token",
    "authjs.session-token",
)

SECRET_ARTIFACT_KEYS = {
    "access_token",
    "accessToken",
    "refresh_token",
    "refreshToken",
    "id_token",
    "idToken",
    "session_token",
    "sessionToken",
    "cookies",
    "cookie",
    "cookie_header",
    "cookieHeader",
    "password",
    "token",
}

K12_RECAPTURE_CONFIG_KEYS = (
    "chatgpt_k12_enabled",
    "chatgpt_k12_workspace_ids",
    "chatgpt_k12_save_all_spaces",
    "chatgpt_k12_strict_join",
    "chatgpt_k12_join_timeout_seconds",
    "chatgpt_k12_join_retry_count",
    "chatgpt_k12_post_join_poll_seconds",
    "chatgpt_k12_capture_refresh_tokens",
)


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enable", "enabled", "y", "开启", "启用"}:
        return True
    if text in {"0", "false", "no", "off", "disable", "disabled", "n", "关闭", "禁用"}:
        return False
    return bool(default)


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(str(value or "").strip()))
    except Exception:
        parsed = int(default)
    return min(max(parsed, minimum), maximum)


def _load_extra(account: AccountModel) -> dict[str, Any]:
    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    return extra if isinstance(extra, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, (dict, list, tuple)):
            try:
                text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                text = str(value or "")
        else:
            text = str(value or "")
        text = text.strip()
        if text:
            return text
    return ""


def access_token_from_account(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _load_extra(account)
    return _first_text(
        extra.get("access_token"),
        extra.get("accessToken"),
        extra.get("webAccessToken"),
        getattr(account, "token", ""),
    )


def session_token_from_account(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _load_extra(account)
    return _first_text(extra.get("session_token"), extra.get("sessionToken"), extra.get("nextauth_session_token"))


def cookies_from_account(account: AccountModel, extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else _load_extra(account)
    return _first_text(extra.get("cookies"), extra.get("cookie_header"), extra.get("cookie"), extra.get("cookie_jar"))


def _fingerprint_impersonate_from_user_agent(user_agent: str, fallback: str = "") -> str:
    match = re.search(r"(?:Chrome|Chromium)/(\d+)", str(user_agent or ""))
    major = int(match.group(1)) if match else 0
    if major >= 136:
        return "chrome136"
    if major >= 133:
        return "chrome133a"
    if major >= 131:
        return "chrome131"
    return fallback or "chrome136"


def _account_browser_fingerprint(account: AccountModel, extra: dict[str, Any]) -> Any:
    registration_context = extra.get("chatgpt_registration_context")
    registration_context = registration_context if isinstance(registration_context, dict) else {}
    browser_fingerprint = registration_context.get("browser_fingerprint")
    browser_fingerprint = browser_fingerprint if isinstance(browser_fingerprint, dict) else {}
    cookies = cookies_from_account(account, extra)
    cookie_device_id = ""
    for item in _iter_cookie_items(cookies):
        if item.get("name") in {"oai-did", "oai-device-id"}:
            cookie_device_id = _safe_str(item.get("value"))
            break
    user_agent = _first_text(
        registration_context.get("user_agent"),
        browser_fingerprint.get("user_agent"),
    )
    sec_ch_ua = _first_text(registration_context.get("sec_ch_ua"), browser_fingerprint.get("sec_ch_ua"))
    impersonate = _first_text(browser_fingerprint.get("impersonate"), registration_context.get("impersonate"))
    impersonate = _fingerprint_impersonate_from_user_agent(user_agent, impersonate)
    return coerce_browser_fingerprint(
        device_id=_first_text(registration_context.get("device_id"), browser_fingerprint.get("device_id"), cookie_device_id),
        user_agent=user_agent or None,
        sec_ch_ua=sec_ch_ua or None,
        impersonate=impersonate,
        accept_language=_first_text(registration_context.get("accept_language"), browser_fingerprint.get("accept_language")) or None,
        platform_version=_first_text(browser_fingerprint.get("platform_version"), registration_context.get("platform_version")) or None,
        viewport_width=browser_fingerprint.get("viewport_width") or registration_context.get("viewport_width") or None,
        viewport_height=browser_fingerprint.get("viewport_height") or registration_context.get("viewport_height") or None,
    )


def _cookie_domain_for_name(name: str) -> str:
    # __Host-* cookies must be host-only by spec; curl_cffi also accepts host domain.
    if str(name or "").startswith("__Host-"):
        return "chatgpt.com"
    return ".chatgpt.com"


def _json_cookie_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        items: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                name = _safe_str(item.get("name") or item.get("key"))
                cookie_value = _safe_str(item.get("value"))
                if name and cookie_value:
                    items.append(
                        {
                            "name": name,
                            "value": cookie_value,
                            "domain": _safe_str(item.get("domain")) or _cookie_domain_for_name(name),
                            "path": _safe_str(item.get("path")) or "/",
                        }
                    )
        return items
    if isinstance(value, dict):
        for key in ("cookies", "cookie_jar", "items"):
            if isinstance(value.get(key), list):
                return _json_cookie_items(value.get(key))
        items = []
        for name, cookie_value in value.items():
            if isinstance(cookie_value, (dict, list)):
                continue
            name_text = _safe_str(name)
            value_text = _safe_str(cookie_value)
            if name_text and value_text:
                items.append(
                    {
                        "name": name_text,
                        "value": value_text,
                        "domain": _cookie_domain_for_name(name_text),
                        "path": "/",
                    }
                )
        return items
    return []


def _iter_cookie_items(cookies: Any) -> list[dict[str, Any]]:
    if isinstance(cookies, (list, dict)):
        return _json_cookie_items(cookies)
    text = str(cookies or "").strip()
    if not text:
        return []
    if text[:1] in "[{":
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        parsed_items = _json_cookie_items(parsed)
        if parsed_items:
            return parsed_items
    items: list[dict[str, Any]] = []
    for part in text.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            continue
        items.append(
            {
                "name": name,
                "value": value,
                "domain": _cookie_domain_for_name(name),
                "path": "/",
            }
        )
    return items


def seed_chatgpt_client_cookies(
    client: ChatGPTClient,
    *,
    cookies: Any = "",
    session_token: str = "",
    device_id: str = "",
) -> dict[str, Any]:
    """Load persisted cookie material into a ChatGPTClient session jar.

    Returns only counts/names, never cookie values.
    """
    items = _iter_cookie_items(cookies)
    names = {_safe_str(item.get("name")) for item in items if _safe_str(item.get("name"))}
    session_token = _safe_str(session_token)
    if session_token and not any(name in names for name in AUTH_COOKIE_NAMES):
        items.append(
            {
                "name": AUTH_COOKIE_NAMES[0],
                "value": session_token,
                "domain": ".chatgpt.com",
                "path": "/",
            }
        )
        names.add(AUTH_COOKIE_NAMES[0])
    device_id = _safe_str(device_id)
    if device_id and "oai-did" not in names:
        items.append({"name": "oai-did", "value": device_id, "domain": ".chatgpt.com", "path": "/"})
        names.add("oai-did")

    loaded = 0
    for item in items:
        name = _safe_str(item.get("name"))
        value = _safe_str(item.get("value"))
        if not name or not value:
            continue
        domain = _safe_str(item.get("domain")) or _cookie_domain_for_name(name)
        path = _safe_str(item.get("path")) or "/"
        try:
            client.session.cookies.set(name, value, domain=domain, path=path)
            loaded += 1
        except Exception:
            try:
                client.session.cookies.set(name, value, domain="chatgpt.com", path="/")
                loaded += 1
            except Exception:
                continue
    return {"loaded": loaded, "names": sorted(names)}


def _normalize_scope(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"k12", "education", "edu", "school"}:
        return "k12"
    if text in {"business", "team", "workspace", "enterprise"}:
        return "business" if text != "workspace" else "workspace"
    if text in {"free", "personal", "default", "individual"}:
        return "free"
    return text


def _artifact_variant_key(artifact: dict[str, Any]) -> str:
    scope = _normalize_scope(artifact.get("scope")) or "workspace"
    workspace_id = _safe_str(artifact.get("workspace_id"))
    account_id = _safe_str(artifact.get("account_id"))
    return _safe_str(artifact.get("variant_key")) or f"{scope}:{workspace_id or account_id or 'default'}"


def safe_artifact_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    """Return a UI/API-safe workspace artifact summary without secrets."""
    space = artifact.get("space") if isinstance(artifact.get("space"), dict) else {}
    join_result = artifact.get("k12_join") if isinstance(artifact.get("k12_join"), dict) else {}
    summary = {
        "scope": _normalize_scope(artifact.get("scope")) or _safe_str(artifact.get("scope")),
        "label": _safe_str(artifact.get("label")),
        "workspace_id": _safe_str(artifact.get("workspace_id")),
        "account_id": _safe_str(artifact.get("account_id")),
        "display_name": _safe_str(artifact.get("display_name") or space.get("name")),
        "source": _safe_str(artifact.get("source")),
        "variant_key": _artifact_variant_key(artifact),
        "auth_level": _safe_str(artifact.get("auth_level")),
        "partial_auth": bool(artifact.get("partial_auth")),
        "has_access_token": bool(_safe_str(artifact.get("access_token"))),
        "has_refresh_token": bool(_safe_str(artifact.get("refresh_token"))),
        "has_session_token": bool(_safe_str(artifact.get("session_token"))),
        "has_cookies": bool(_safe_str(artifact.get("cookies") or artifact.get("cookie_header"))),
    }
    if space:
        summary["space"] = {
            "workspace_id": _safe_str(space.get("workspace_id")),
            "account_id": _safe_str(space.get("account_id")),
            "name": _safe_str(space.get("name")),
            "structure": _safe_str(space.get("structure")),
            "plan_type": _safe_str(space.get("plan_type")),
            "is_default": bool(space.get("is_default")),
            "source": _safe_str(space.get("source")),
        }
    if join_result:
        summary["k12_join"] = {
            "workspace_id": _safe_str(join_result.get("workspace_id")),
            "ok": bool(join_result.get("ok")),
            "status_code": int(join_result.get("status_code") or 0),
            "message": safe_k12_error(join_result.get("message"), 180),
            "error_code": _safe_str(join_result.get("error_code")),
            "already_joined": bool(join_result.get("already_joined")),
        }
    return summary


def _workspace_variants_from_artifacts(artifacts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        summary = safe_artifact_summary(artifact)
        variant = {
            "scope": summary.get("scope") or "",
            "label": summary.get("label") or "",
            "workspace_id": summary.get("workspace_id") or "",
            "account_id": summary.get("account_id") or "",
            "display_name": summary.get("display_name") or "",
            "source": summary.get("source") or "",
            "auth_level": summary.get("auth_level") or "",
            "partial_auth": bool(summary.get("partial_auth")),
        }
        key = (variant["scope"], variant["workspace_id"], variant["account_id"])
        if key in seen:
            continue
        seen.add(key)
        variants.append(variant)
    return variants[:50]


def _find_matching_artifact(account: AccountModel, extra: dict[str, Any], artifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not artifacts:
        return None
    current_variant_key = _safe_str(extra.get("chatgpt_workspace_variant_key"))
    current_workspace_id = _safe_str(extra.get("workspace_id") or extra.get("organization_id"))
    current_account_id = _safe_str(extra.get("account_id") or getattr(account, "user_id", ""))
    current_scope = _normalize_scope(extra.get("chatgpt_workspace_scope"))
    if current_variant_key:
        for artifact in artifacts:
            if _artifact_variant_key(artifact) == current_variant_key:
                return artifact
    for artifact in artifacts:
        artifact_workspace = _safe_str(artifact.get("workspace_id"))
        artifact_account = _safe_str(artifact.get("account_id"))
        if current_workspace_id and current_workspace_id in {artifact_workspace, artifact_account}:
            return artifact
        if current_account_id and current_account_id in {artifact_workspace, artifact_account}:
            return artifact
    if not current_scope or current_scope == "free":
        for artifact in artifacts:
            if (_normalize_scope(artifact.get("scope")) or "free") == "free":
                return artifact
    return None


def _copy_if_present(target: dict[str, Any], source: dict[str, Any], key: str, *aliases: str) -> None:
    value = _first_text(*(source.get(alias) for alias in (key, *aliases)))
    if value:
        target[key] = value


def _build_artifact_extra(
    artifact: dict[str, Any],
    *,
    source_account: AccountModel,
    source_extra: dict[str, Any],
    existing_extra: dict[str, Any] | None = None,
    captured_at: str,
) -> dict[str, Any]:
    existing_extra = existing_extra if isinstance(existing_extra, dict) else {}
    scope = _normalize_scope(artifact.get("scope")) or "workspace"
    space = artifact.get("space") if isinstance(artifact.get("space"), dict) else {}
    display_name = _safe_str(artifact.get("display_name") or space.get("name") or artifact.get("label") or scope)
    refresh_token = _first_text(artifact.get("refresh_token"), existing_extra.get("refresh_token"), existing_extra.get("refreshToken"))
    extra = {
        "access_token": _first_text(artifact.get("access_token"), existing_extra.get("access_token"), existing_extra.get("accessToken")),
        "refresh_token": refresh_token,
        "id_token": _first_text(artifact.get("id_token"), existing_extra.get("id_token"), existing_extra.get("idToken")),
        "session_token": _first_text(artifact.get("session_token"), existing_extra.get("session_token"), existing_extra.get("sessionToken")),
        "workspace_id": _safe_str(artifact.get("workspace_id") or space.get("workspace_id")),
        "account_id": _safe_str(artifact.get("account_id") or space.get("account_id")),
        "chatgpt_registration_mode": _safe_str(existing_extra.get("chatgpt_registration_mode")) or "access_token_only",
        "chatgpt_has_refresh_token_solution": bool(refresh_token),
        "chatgpt_token_source": _safe_str(artifact.get("source")) or "k12_manual_recapture",
        "chatgpt_workspace_scope": scope,
        "chatgpt_workspace_label": _safe_str(artifact.get("label")) or scope,
        "chatgpt_workspace_display_name": display_name,
        "chatgpt_workspace_variant_key": _artifact_variant_key(artifact),
        "auth_level": _safe_str(artifact.get("auth_level")) or ("refresh_token" if refresh_token else "access_token_only"),
        "partial_auth": bool(artifact.get("partial_auth") or not refresh_token),
        "chatgpt_k12_recaptured_from_account_id": int(getattr(source_account, "id", 0) or 0),
        "chatgpt_k12_recaptured_at": captured_at,
    }
    cookies = _first_text(artifact.get("cookies"), artifact.get("cookie_header"), existing_extra.get("cookies"), existing_extra.get("cookie_header"))
    if cookies:
        extra["cookies"] = cookies
        extra["cookie_header"] = cookies
    if space:
        extra["chatgpt_workspace_space"] = space
    if isinstance(artifact.get("k12_join"), dict):
        extra["chatgpt_k12_join"] = artifact.get("k12_join")
    if artifact.get("all_spaces_capture"):
        extra["chatgpt_all_spaces_capture"] = True

    # Preserve non-secret operational context that helps later browser/auth actions.
    for key in (
        "chatgpt_registration_context",
        "registration_web_session_material_preserved",
        "mail_provider",
        "chatgpt_mailbox_state",
        "chatgpt_browser_auth",
    ):
        if key in existing_extra and key not in extra:
            extra[key] = existing_extra.get(key)
        elif key in source_extra and key not in extra:
            extra[key] = source_extra.get(key)
    return extra


def _merge_account_extra_for_artifact(
    account: AccountModel,
    artifact: dict[str, Any] | None,
    *,
    source_extra: dict[str, Any],
    captured_at: str,
    workspace_variants: list[dict[str, Any]],
    capture: dict[str, Any],
    request_summary: dict[str, Any],
) -> dict[str, Any]:
    existing_extra = _load_extra(account)
    next_extra = dict(existing_extra)
    if artifact:
        artifact_extra = _build_artifact_extra(
            artifact,
            source_account=account,
            source_extra=source_extra,
            existing_extra=existing_extra,
            captured_at=captured_at,
        )
        for key, value in artifact_extra.items():
            # Never erase an existing RT with an AT-only recapture artifact.
            if key == "refresh_token" and not _safe_str(value) and _safe_str(existing_extra.get("refresh_token")):
                continue
            if value not in (None, "", [], {}):
                next_extra[key] = value
        if _safe_str(artifact_extra.get("access_token")):
            account.token = _safe_str(artifact_extra.get("access_token"))
        if _safe_str(artifact_extra.get("account_id")):
            account.user_id = _safe_str(artifact_extra.get("account_id"))

    summary = capture.get("summary") if isinstance(capture.get("summary"), dict) else {}
    next_extra["chatgpt_k12_join_summary"] = dict(summary)
    next_extra["chatgpt_k12_join_results"] = capture.get("join_results") or []
    next_extra["chatgpt_all_spaces"] = capture.get("spaces") or []
    next_extra["chatgpt_workspace_variants"] = workspace_variants
    if capture.get("exchange_failures"):
        next_extra["chatgpt_k12_exchange_failures"] = capture.get("exchange_failures")
    else:
        next_extra.pop("chatgpt_k12_exchange_failures", None)
    next_extra["chatgpt_k12_manual_recapture"] = {
        **request_summary,
        "captured_at": captured_at,
        "summary": dict(summary),
        "artifacts": workspace_variants,
    }
    history = next_extra.get("chatgpt_k12_recapture_history")
    history = list(history) if isinstance(history, list) else []
    history.append(next_extra["chatgpt_k12_manual_recapture"])
    next_extra["chatgpt_k12_recapture_history"] = history[-20:]
    account.set_extra(next_extra)
    next_extra["chatgpt_capabilities"] = classify_chatgpt_capabilities(account)
    account.set_extra(next_extra)
    account.updated_at = datetime.now(timezone.utc)
    return next_extra


def _existing_variant_account(
    session: Session,
    *,
    email: str,
    variant_key: str,
) -> AccountModel | None:
    candidates = session.exec(
        select(AccountModel)
        .where(AccountModel.platform == "chatgpt")
        .where(AccountModel.email == email)
    ).all()
    for candidate in candidates:
        extra = _load_extra(candidate)
        if _safe_str(extra.get("chatgpt_workspace_variant_key")) == variant_key:
            return candidate
    return None


def _status_for_recaptured_account(existing: AccountModel | None, artifact: dict[str, Any]) -> str:
    if existing is not None and _safe_str(existing.status):
        return _safe_str(existing.status)
    if _safe_str(artifact.get("refresh_token")):
        return "registered"
    return "pending_payment"


def _upsert_artifact_account(
    session: Session,
    *,
    source_account: AccountModel,
    source_extra: dict[str, Any],
    artifact: dict[str, Any],
    captured_at: str,
    current_account: AccountModel,
) -> AccountModel:
    variant_key = _artifact_variant_key(artifact)
    current_extra = _load_extra(current_account)
    current_variant_key = _safe_str(current_extra.get("chatgpt_workspace_variant_key"))
    current_is_unkeyed_free = not current_variant_key and (_normalize_scope(artifact.get("scope")) or "free") == "free"
    if variant_key and variant_key == current_variant_key or current_is_unkeyed_free:
        target = current_account
    else:
        target = _existing_variant_account(session, email=_safe_str(source_account.email), variant_key=variant_key)

    existing_extra = _load_extra(target) if target is not None else {}
    artifact_extra = _build_artifact_extra(
        artifact,
        source_account=source_account,
        source_extra=source_extra,
        existing_extra=existing_extra,
        captured_at=captured_at,
    )
    if target is None:
        target = AccountModel(
            platform="chatgpt",
            email=_safe_str(source_account.email),
            password=_safe_str(source_account.password),
            user_id=_safe_str(artifact_extra.get("account_id")),
            region=_safe_str(source_account.region),
            token=_safe_str(artifact_extra.get("access_token")),
            status=_status_for_recaptured_account(None, artifact),
            cashier_url=_safe_str(artifact_extra.get("cashier_url")),
        )
    else:
        target.password = _safe_str(target.password or source_account.password)
        target.region = _safe_str(target.region or source_account.region)
        target.status = _status_for_recaptured_account(target, artifact)
        if _safe_str(artifact_extra.get("account_id")):
            target.user_id = _safe_str(artifact_extra.get("account_id"))
        if _safe_str(artifact_extra.get("access_token")):
            target.token = _safe_str(artifact_extra.get("access_token"))

    merged_extra = dict(existing_extra)
    for key, value in artifact_extra.items():
        if key == "refresh_token" and not _safe_str(value) and _safe_str(existing_extra.get("refresh_token")):
            continue
        if value not in (None, "", [], {}):
            merged_extra[key] = value
    target.set_extra(merged_extra)
    merged_extra["chatgpt_capabilities"] = classify_chatgpt_capabilities(target)
    target.set_extra(merged_extra)
    target.updated_at = datetime.now(timezone.utc)
    session.add(target)
    session.flush()
    return target


def _redact_capture_for_response(capture: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": capture.get("summary") if isinstance(capture.get("summary"), dict) else {},
        "spaces": capture.get("spaces") or [],
        "join_results": capture.get("join_results") or [],
        "exchange_failures": capture.get("exchange_failures") or [],
        "accounts_check": capture.get("accounts_check") if isinstance(capture.get("accounts_check"), dict) else {},
    }


def recapture_saved_account_k12_workspaces(
    *,
    session: Session,
    account: AccountModel,
    config: dict[str, Any] | None = None,
    workspace_ids: Any = None,
    save_all_spaces: bool | None = None,
    strict_join: bool | None = None,
    proxy: str = "",
    log_fn: Any = None,
    stop_checker: Any = None,
) -> dict[str, Any]:
    if _safe_str(account.platform).lower() != "chatgpt":
        raise ValueError("只有 ChatGPT 账号支持 K12/workspace 重新捕获")

    source_extra = _load_extra(account)
    access_token = access_token_from_account(account, source_extra)
    session_token = session_token_from_account(account, source_extra)
    cookies = cookies_from_account(account, source_extra)
    if not access_token:
        raise ValueError("当前账号缺少已保存 access_token")
    if not cookies and not session_token:
        raise ValueError("当前账号缺少已保存 cookies/session_token，无法重新进入 workspace")
    if not cookies and session_token:
        cookies = f"{AUTH_COOKIE_NAMES[0]}={session_token}"

    config = dict(config or {})
    if workspace_ids is not None:
        config["chatgpt_k12_workspace_ids"] = workspace_ids
    if save_all_spaces is not None:
        config["chatgpt_k12_save_all_spaces"] = bool(save_all_spaces)
    else:
        config["chatgpt_k12_save_all_spaces"] = _truthy(config.get("chatgpt_k12_save_all_spaces"), default=True)
    if strict_join is not None:
        config["chatgpt_k12_strict_join"] = bool(strict_join)
    config["chatgpt_k12_enabled"] = True
    config.setdefault("chatgpt_k12_join_timeout_seconds", 60)
    config.setdefault("chatgpt_k12_join_retry_count", 2)
    config.setdefault("chatgpt_k12_post_join_poll_seconds", "3,8,15")
    config["chatgpt_k12_capture_refresh_tokens"] = False

    target_ids = parse_k12_workspace_ids(config.get("chatgpt_k12_workspace_ids"))
    captured_at = _utcnow_iso()
    fingerprint = _account_browser_fingerprint(account, source_extra)
    client = ChatGPTClient(proxy=_safe_str(proxy), verbose=False, fingerprint=fingerprint)
    cookie_seed = seed_chatgpt_client_cookies(
        client,
        cookies=cookies,
        session_token=session_token,
        device_id=getattr(fingerprint, "device_id", ""),
    )
    logs: list[dict[str, str]] = []

    def _log(message: str, level: str = "info") -> None:
        if callable(stop_checker):
            stop_checker()
        level_text = _safe_str(level) or "info"
        message_text = safe_k12_error(message, 240)
        logs.append({"level": level_text, "message": message_text})
        if callable(log_fn):
            try:
                log_fn(message_text, level_text)
            except TypeError:
                log_fn(message_text)

    try:
        capture = capture_k12_and_all_spaces(
            chatgpt_client=client,
            base_session={
                "access_token": access_token,
                "session_token": session_token,
                "cookies": cookies,
                "cookie_header": cookies,
            },
            access_token=access_token,
            session_token=session_token,
            cookies=cookies,
            target_workspace_ids=config.get("chatgpt_k12_workspace_ids"),
            proxy=_safe_str(proxy),
            config=config,
            log_fn=_log,
            stop_checker=stop_checker,
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    artifacts = [dict(item) for item in (capture.get("artifacts") or []) if isinstance(item, dict)]
    workspace_variants = _workspace_variants_from_artifacts(artifacts)
    request_summary = {
        "source_account_id": int(account.id or 0),
        "target_workspace_ids": target_ids,
        "target_count": len(target_ids),
        "save_all_spaces": _truthy(config.get("chatgpt_k12_save_all_spaces"), default=True),
        "strict_join": _truthy(config.get("chatgpt_k12_strict_join"), default=False),
    }

    matched_artifact = _find_matching_artifact(account, source_extra, artifacts)
    _merge_account_extra_for_artifact(
        account,
        matched_artifact,
        source_extra=source_extra,
        captured_at=captured_at,
        workspace_variants=workspace_variants,
        capture=capture,
        request_summary=request_summary,
    )
    session.add(account)
    session.flush()

    saved_accounts: list[AccountModel] = []
    for artifact in artifacts:
        saved = _upsert_artifact_account(
            session,
            source_account=account,
            source_extra=source_extra,
            artifact=artifact,
            captured_at=captured_at,
            current_account=account,
        )
        saved_accounts.append(saved)

    # Ensure current account is included even when no artifact matched/saved.
    changed_ids: list[int] = []
    for row in [account, *saved_accounts]:
        row_id = int(getattr(row, "id", 0) or 0)
        if row_id > 0 and row_id not in changed_ids:
            changed_ids.append(row_id)
    if changed_ids:
        upsert_account_list_state_for_account_ids(session, changed_ids, commit=False)
    session.commit()
    for row in [account, *saved_accounts]:
        try:
            session.refresh(row)
        except Exception:
            pass

    summary = capture.get("summary") if isinstance(capture.get("summary"), dict) else {}
    exported_count = len(artifacts)
    summary.setdefault("exported_artifacts", exported_count)
    summary.setdefault("saved_artifacts", exported_count)
    ok = not bool(summary.get("strict_join_failed")) and exported_count > 0
    if not ok and exported_count <= 0:
        summary.setdefault(
            "error",
            "K12 / Workspace 重跑未导出任何可写回的 workspace token；join 成功不等于导出成功",
        )
    return {
        "ok": ok,
        "account_id": int(account.id or 0),
        "email": _safe_str(account.email),
        "captured_at": captured_at,
        "target_workspace_ids": target_ids,
        "cookie_seed": {"loaded": int(cookie_seed.get("loaded") or 0), "names": list(cookie_seed.get("names") or [])},
        "summary": summary,
        "capture": _redact_capture_for_response(capture),
        "artifacts": [safe_artifact_summary(item) for item in artifacts],
        "saved_accounts": [
            {
                "id": int(row.id or 0),
                "email": _safe_str(row.email),
                "status": _safe_str(row.status),
                "workspace_variant_key": _safe_str(_load_extra(row).get("chatgpt_workspace_variant_key")),
                "workspace_scope": _safe_str(_load_extra(row).get("chatgpt_workspace_scope")),
                "workspace_id": _safe_str(_load_extra(row).get("workspace_id")),
                "account_id": _safe_str(_load_extra(row).get("account_id") or row.user_id),
            }
            for row in saved_accounts
        ],
        "changed_account_ids": changed_ids,
        "logs": logs[-200:],
    }


__all__ = [
    "K12_RECAPTURE_CONFIG_KEYS",
    "access_token_from_account",
    "cookies_from_account",
    "recapture_saved_account_k12_workspaces",
    "safe_artifact_summary",
    "seed_chatgpt_client_cookies",
    "session_token_from_account",
]
