"""ChatGPT authentication lifecycle state.

The account table remains the owner of secret material.  This module owns only
non-secret timing, probe evidence, refresh outcomes and compatibility
projections used by the API/UI.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


LIFECYCLE_EXTRA_KEY = "chatgpt_auth_lifecycle"
LIFECYCLE_SCHEMA_VERSION = 3
ACCESS_TOKEN_ONLY_LIFETIME_SECONDS = 10 * 24 * 60 * 60
ACCESS_TOKEN_ONLY_EXPIRY_SOURCE = "at_only_10d_policy"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(access_token|refresh_token|session_token)=([^\s&;]+)", r"\1=[redacted]", text)
    return text[:limit]


def _as_epoch(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 100_000_000_000:
            number /= 1000.0
        return number if number > 0 else None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return _as_epoch(float(raw))
    except (TypeError, ValueError):
        pass
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def iso_from_value(value: Any) -> str:
    epoch = _as_epoch(value)
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def epoch_from_value(value: Any) -> float | None:
    """Public numeric conversion for downstream export adapters."""

    return _as_epoch(value)


def decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def token_timing(token: str) -> dict[str, str]:
    payload = decode_jwt_payload(token)
    exp = iso_from_value(payload.get("exp"))
    issued = iso_from_value(payload.get("iat"))
    return {
        "issued_at": issued,
        "expires_at": exp,
        "expiry_source": "jwt_exp" if exp else "",
        "expiry_confidence": "exact" if exp else "unknown",
    }


def _material_revision(credentials: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(
            [
                credentials["access_token"],
                credentials["refresh_token"],
                credentials["session_token"],
                credentials["cookies"],
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _capture_timestamp(account: Any, values: dict[str, Any], fallback: str) -> str:
    for candidate in (
        values.get("access_token_captured_at"),
        values.get("registered_at"),
        values.get("created_at"),
        getattr(account, "created_at", ""),
    ):
        normalized = iso_from_value(candidate)
        if normalized:
            return normalized
    return fallback


def access_token_timing(
    account: Any,
    values: dict[str, Any],
    credentials: dict[str, str],
    *,
    captured_at: Any = "",
    allow_at_only_policy: bool = True,
) -> dict[str, str]:
    """Prefer exact timing, then return an explicitly-labelled AT-only estimate."""

    timing = token_timing(credentials["access_token"])
    explicit_issued = iso_from_value(values.get("access_token_issued_at"))
    if explicit_issued and not timing.get("issued_at"):
        timing["issued_at"] = explicit_issued
    explicit_expiry = iso_from_value(values.get("access_token_expires_at"))
    if not timing.get("expires_at") and explicit_expiry:
        expiry_source = str(values.get("access_token_expiry_source") or "oauth_expires_in")
        timing.update(
            {
                "expires_at": explicit_expiry,
                "expiry_source": expiry_source,
                "expiry_confidence": "estimated" if expiry_source == ACCESS_TOKEN_ONLY_EXPIRY_SOURCE else "exact",
            }
        )
    if timing.get("expires_at") or not credentials["access_token"]:
        return timing
    if not allow_at_only_policy or credentials["refresh_token"]:
        return timing

    captured = iso_from_value(captured_at) or _capture_timestamp(account, values, utc_now_iso())
    captured_epoch = _as_epoch(captured)
    if captured_epoch is None:
        return timing
    issued_at = timing.get("issued_at") or captured
    return {
        **timing,
        "issued_at": issued_at,
        "expires_at": iso_from_value(captured_epoch + ACCESS_TOKEN_ONLY_LIFETIME_SECONDS),
        "expiry_source": ACCESS_TOKEN_ONLY_EXPIRY_SOURCE,
        "expiry_confidence": "estimated",
    }


def normalize_plan(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if "enterprise" in raw:
        return "enterprise"
    if "team" in raw or "business" in raw:
        return "team"
    if "pro" in raw:
        return "pro"
    if "plus" in raw:
        return "plus"
    if "free" in raw:
        return "free"
    return "unknown"


def is_deactivated_evidence(error_code: Any, message: Any) -> bool:
    text = f"{error_code} {message}".strip().lower()
    return any(
        marker in text
        for marker in (
            "account_deactivated",
            "account deleted",
            "account disabled",
            "deleted or deactivated",
            "deactivated workspace",
            "workspace_deactivated",
        )
    )


def classify_access_probe(status_code: Any, error_code: Any, message: Any) -> tuple[str, str]:
    try:
        status = int(status_code or 0)
    except (TypeError, ValueError):
        status = 0
    code = str(error_code or "").strip().lower()
    if status == 200:
        return "valid", "active_confirmed"
    if status == 401:
        if code == "token_expired":
            return "expired", "unknown"
        if code in {"token_invalidated", "token_revoked", "invalid_token", "invalid_grant"}:
            return "revoked", "unknown"
        return "unauthorized_unknown", "unknown"
    if status in {402, 403}:
        if is_deactivated_evidence(code, message):
            return "rejected", "deactivated_confirmed"
        if status == 403:
            return "rejected", "banned_suspected"
        return "probe_failed", "unknown"
    if status == 429 or status == 0 or status >= 500:
        return "probe_failed", "unknown"
    return "probe_failed", "unknown"


def classify_refresh_attempt(attempt: dict[str, Any] | None) -> tuple[str, str]:
    item = attempt if isinstance(attempt, dict) else {}
    if not bool(item.get("attempted")):
        return "not_attempted", "not_attempted"
    if bool(item.get("success")):
        return "success", "valid"
    code = str(item.get("error_code") or "").strip().lower()
    try:
        status = int(item.get("http_status") or 0)
    except (TypeError, ValueError):
        status = 0
    message = str(item.get("message") or "").lower()
    if code in {"token_revoked", "token_invalidated", "invalid_grant", "invalid_token"}:
        return "rejected", "rejected"
    if status in {401, 403}:
        return "rejected", "rejected"
    if status == 429 or status == 0 or status >= 500 or any(
        marker in message for marker in ("timeout", "timed out", "proxy", "connection")
    ):
        return "failed_transient", "failed_transient"
    return "failed", "unknown"


def derive_state(
    *,
    at_state: str,
    rt_state: str,
    account_evidence_state: str,
    at_present: bool,
    rt_present: bool,
) -> tuple[str, str]:
    if account_evidence_state == "deactivated_confirmed":
        return "account_deactivated", "blocked_account_deactivated"
    if account_evidence_state == "banned_suspected":
        return "account_blocked_suspected", "blocked_account_suspected"
    if rt_state == "valid" and at_state in {"valid", "expired", "unknown"}:
        return "rt_backed", "ready"
    if rt_state in {"rejected", "failed_transient", "failed"} and at_state == "valid":
        return "refresh_failed_at_valid", "degraded_at_fallback"
    if rt_state in {"rejected", "failed_transient", "failed"} and (
        at_state in {"expired", "revoked", "unauthorized_unknown", "not_present"}
        or not at_present
    ):
        return "refresh_failed_at_unusable", "blocked_auth_material"
    if rt_state in {"rejected", "failed_transient", "failed"} and at_state == "unknown" and at_present:
        return "refresh_failed_at_unknown", "degraded_at_unprobed"
    if not rt_present and at_state == "valid":
        return "at_only_valid", "blocked_missing_rt"
    if not rt_present and at_state == "expired":
        return "at_only_expired", "blocked_at_expired"
    if at_state == "revoked":
        return "at_revoked", "blocked_at_revoked"
    if at_state == "expired":
        return "at_expired", "blocked_at_expired"
    if not at_present and not rt_present:
        return "no_auth_material", "blocked_missing_at"
    if rt_state == "not_attempted":
        return "not_checked", "unknown"
    return "unknown", "unknown"


def _credentials(account: Any, extra: dict[str, Any]) -> dict[str, str]:
    return {
        "access_token": str(
            extra.get("access_token")
            or extra.get("accessToken")
            or getattr(account, "access_token", "")
            or getattr(account, "token", "")
            or ""
        ).strip(),
        "refresh_token": str(
            extra.get("refresh_token")
            or extra.get("refreshToken")
            or getattr(account, "refresh_token", "")
            or ""
        ).strip(),
        "session_token": str(extra.get("session_token") or extra.get("sessionToken") or "").strip(),
        "cookies": str(extra.get("cookies") or extra.get("cookie_header") or "").strip(),
    }


def build_account_lifecycle_projection(account: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    values = extra if isinstance(extra, dict) else {}
    credentials = _credentials(account, values)
    now = utc_now_iso()
    captured_at = _capture_timestamp(account, values, now)
    timing = access_token_timing(account, values, credentials, captured_at=captured_at)
    at_state = "unknown"
    if not credentials["access_token"]:
        at_state = "not_present"
    elif timing.get("expires_at") and _as_epoch(timing["expires_at"]) <= _as_epoch(now):
        at_state = "expired"
    local_probe = values.get("chatgpt_local") if isinstance(values.get("chatgpt_local"), dict) else {}
    local_auth = local_probe.get("auth") if isinstance(local_probe.get("auth"), dict) else {}
    legacy_auth_state = str(local_auth.get("state") or "").strip().lower()
    legacy_auth_code = str(local_auth.get("error_code") or "").strip().lower()
    if legacy_auth_code in {"token_invalidated", "token_revoked"}:
        at_state = "revoked"
    elif legacy_auth_code == "token_expired" or legacy_auth_state == "access_token_expired":
        at_state = "expired"
    elif (
        credentials["access_token"]
        and legacy_auth_state in {"refresh_token_valid", "access_token_valid"}
        and at_state != "expired"
    ):
        at_state = "valid"
    legacy_evidence_state = "unknown"
    if legacy_auth_state == "account_deactivated":
        legacy_evidence_state = "deactivated_confirmed"
    elif legacy_auth_state == "banned_like":
        legacy_evidence_state = "banned_suspected"
    return {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "material_revision": _material_revision(credentials),
        "material": {
            "type": "refresh_token" if credentials["refresh_token"] else "access_token_only" if credentials["access_token"] else "unknown",
            "has_access_token": bool(credentials["access_token"]),
            "has_refresh_token": bool(credentials["refresh_token"]),
            "has_session_token": bool(credentials["session_token"]),
            "has_cookies": bool(credentials["cookies"]),
        },
        "access_token": {
            "state": at_state,
            "issued_at": timing.get("issued_at", ""),
            "expires_at": timing.get("expires_at", ""),
            "expiry_source": timing.get("expiry_source", ""),
            "expiry_confidence": timing.get("expiry_confidence", "unknown"),
            "observed_at": captured_at if credentials["access_token"] else "",
            "last_probe_at": "",
            "last_http_status": 0,
            "last_error_code": "",
        },
        "refresh_token": {
            "state": "not_attempted" if credentials["refresh_token"] else "not_present",
            "last_attempt_at": "",
            "last_success_at": "",
            "last_failure_at": "",
            "last_result": "not_attempted" if credentials["refresh_token"] else "not_present",
            "last_http_status": 0,
            "last_error_code": "",
            "last_error_message": "",
        },
        "web_session": {
            "expires_at": str(values.get("web_session_expires_at") or values.get("web_session_expires") or "").strip(),
            "expiry_source": str(values.get("web_session_expiry_source") or "").strip(),
            "observed_at": str(values.get("web_session_observed_at") or "").strip(),
        },
        "account_evidence": {
            "state": legacy_evidence_state,
            "code": str(local_auth.get("error_code") or ""),
            "message": _safe_text(local_auth.get("message")),
            "at": str(local_auth.get("checked_at") or ""),
        },
        "probe": {
            "state": "never_checked",
            "checked_at": "",
            "transport": "",
            "error_code": "",
            "error_message": "",
        },
        "subscription": {
            "current_plan": normalize_plan((local_probe.get("subscription") or {}).get("plan")) if isinstance(local_probe.get("subscription"), dict) else "unknown",
            "current_active_until": str((local_probe.get("subscription") or {}).get("subscription_active_until") or "") if isinstance(local_probe.get("subscription"), dict) else "",
            "current_checked_at": str((local_probe.get("subscription") or {}).get("checked_at") or "") if isinstance(local_probe.get("subscription"), dict) else "",
            "current_state": "confirmed" if isinstance(local_probe.get("subscription"), dict) and normalize_plan((local_probe.get("subscription") or {}).get("plan")) != "unknown" and at_state == "valid" else "not_checked",
            "last_confirmed_plan": normalize_plan((local_probe.get("subscription") or {}).get("plan")) if isinstance(local_probe.get("subscription"), dict) else "",
            "last_confirmed_active_until": str((local_probe.get("subscription") or {}).get("subscription_active_until") or "") if isinstance(local_probe.get("subscription"), dict) else "",
            "last_confirmed_at": str((local_probe.get("subscription") or {}).get("checked_at") or "") if isinstance(local_probe.get("subscription"), dict) else "",
        },
        "derived": {
            "state": derive_state(
                at_state=at_state,
                rt_state="not_attempted" if credentials["refresh_token"] else "not_present",
                account_evidence_state=legacy_evidence_state,
                at_present=bool(credentials["access_token"]),
                rt_present=bool(credentials["refresh_token"]),
            )[0],
            "availability": derive_state(
                at_state=at_state,
                rt_state="not_attempted" if credentials["refresh_token"] else "not_present",
                account_evidence_state=legacy_evidence_state,
                at_present=bool(credentials["access_token"]),
                rt_present=bool(credentials["refresh_token"]),
            )[1],
        },
        "updated_at": now,
    }


def _model_to_projection(row: Any) -> dict[str, Any]:
    return {
        "schema_version": int(getattr(row, "schema_version", LIFECYCLE_SCHEMA_VERSION) or LIFECYCLE_SCHEMA_VERSION),
        "material_revision": str(getattr(row, "material_revision", "") or ""),
        "material": {
            "type": "refresh_token" if getattr(row, "refresh_token_present", False) else "access_token_only" if getattr(row, "access_token_present", False) else "unknown",
            "has_access_token": bool(getattr(row, "access_token_present", False)),
            "has_refresh_token": bool(getattr(row, "refresh_token_present", False)),
            "has_session_token": bool(getattr(row, "session_token_present", False)),
            "has_cookies": bool(getattr(row, "cookies_present", False)),
        },
        "access_token": {
            "state": str(getattr(row, "access_token_state", "unknown") or "unknown"),
            "issued_at": str(getattr(row, "access_token_issued_at", "") or ""),
            "expires_at": str(getattr(row, "access_token_expires_at", "") or ""),
            "expiry_source": str(getattr(row, "access_token_expiry_source", "") or ""),
            "expiry_confidence": str(getattr(row, "access_token_expiry_confidence", "unknown") or "unknown"),
            "observed_at": str(getattr(row, "access_token_observed_at", "") or ""),
            "last_probe_at": str(getattr(row, "access_token_last_probe_at", "") or ""),
            "last_http_status": int(getattr(row, "access_token_last_http_status", 0) or 0),
            "last_error_code": str(getattr(row, "access_token_last_error_code", "") or ""),
        },
        "refresh_token": {
            "state": str(getattr(row, "refresh_token_state", "unknown") or "unknown"),
            "last_attempt_at": str(getattr(row, "refresh_token_last_attempt_at", "") or ""),
            "last_success_at": str(getattr(row, "refresh_token_last_success_at", "") or ""),
            "last_failure_at": str(getattr(row, "refresh_token_last_failure_at", "") or ""),
            "last_result": str(getattr(row, "refresh_token_last_result", "not_attempted") or "not_attempted"),
            "last_http_status": int(getattr(row, "refresh_token_last_http_status", 0) or 0),
            "last_error_code": str(getattr(row, "refresh_token_last_error_code", "") or ""),
            "last_error_message": _safe_text(getattr(row, "refresh_token_last_error_message", "")),
        },
        "web_session": {
            "expires_at": str(getattr(row, "web_session_expires_at", "") or ""),
            "expiry_source": str(getattr(row, "web_session_expiry_source", "") or ""),
            "observed_at": str(getattr(row, "web_session_observed_at", "") or ""),
        },
        "account_evidence": {
            "state": str(getattr(row, "account_evidence_state", "unknown") or "unknown"),
            "code": str(getattr(row, "account_evidence_code", "") or ""),
            "message": _safe_text(getattr(row, "account_evidence_message", "")),
            "at": str(getattr(row, "account_evidence_at", "") or ""),
        },
        "probe": {
            "state": str(getattr(row, "probe_state", "never_checked") or "never_checked"),
            "checked_at": str(getattr(row, "probe_checked_at", "") or ""),
            "transport": str(getattr(row, "probe_transport", "") or ""),
            "error_code": str(getattr(row, "probe_error_code", "") or ""),
            "error_message": _safe_text(getattr(row, "probe_error_message", "")),
        },
        "derived": {
            "state": str(getattr(row, "derived_state", "unknown") or "unknown"),
            "availability": str(getattr(row, "availability_state", "unknown") or "unknown"),
        },
        "updated_at": iso_from_value(getattr(row, "updated_at", "")),
    }


def _set_model_from_projection(row: Any, projection: dict[str, Any]) -> None:
    material = projection.get("material") if isinstance(projection.get("material"), dict) else {}
    access = projection.get("access_token") if isinstance(projection.get("access_token"), dict) else {}
    refresh = projection.get("refresh_token") if isinstance(projection.get("refresh_token"), dict) else {}
    web = projection.get("web_session") if isinstance(projection.get("web_session"), dict) else {}
    evidence = projection.get("account_evidence") if isinstance(projection.get("account_evidence"), dict) else {}
    probe = projection.get("probe") if isinstance(projection.get("probe"), dict) else {}
    derived = projection.get("derived") if isinstance(projection.get("derived"), dict) else {}
    row.schema_version = LIFECYCLE_SCHEMA_VERSION
    row.material_revision = str(projection.get("material_revision") or "")
    row.access_token_present = bool(material.get("has_access_token"))
    row.access_token_state = str(access.get("state") or "unknown")
    row.access_token_issued_at = str(access.get("issued_at") or "")
    row.access_token_expires_at = str(access.get("expires_at") or "")
    row.access_token_expiry_source = str(access.get("expiry_source") or "")
    row.access_token_expiry_confidence = str(access.get("expiry_confidence") or "unknown")
    row.access_token_observed_at = str(access.get("observed_at") or "")
    row.access_token_last_probe_at = str(access.get("last_probe_at") or "")
    row.access_token_last_http_status = int(access.get("last_http_status") or 0)
    row.access_token_last_error_code = str(access.get("last_error_code") or "")
    row.refresh_token_present = bool(material.get("has_refresh_token"))
    row.refresh_token_state = str(refresh.get("state") or "unknown")
    row.refresh_token_expires_at = str(refresh.get("expires_at") or "")
    row.refresh_token_expiry_source = str(refresh.get("expiry_source") or "")
    row.refresh_token_last_attempt_at = str(refresh.get("last_attempt_at") or "")
    row.refresh_token_last_success_at = str(refresh.get("last_success_at") or "")
    row.refresh_token_last_failure_at = str(refresh.get("last_failure_at") or "")
    row.refresh_token_last_result = str(refresh.get("last_result") or "not_attempted")
    row.refresh_token_last_http_status = int(refresh.get("last_http_status") or 0)
    row.refresh_token_last_error_code = str(refresh.get("last_error_code") or "")
    row.refresh_token_last_error_message = _safe_text(refresh.get("last_error_message"))
    row.session_token_present = bool(material.get("has_session_token"))
    row.cookies_present = bool(material.get("has_cookies"))
    row.web_session_expires_at = str(web.get("expires_at") or "")
    row.web_session_expiry_source = str(web.get("expiry_source") or "")
    row.web_session_observed_at = str(web.get("observed_at") or "")
    row.account_evidence_state = str(evidence.get("state") or "unknown")
    row.account_evidence_code = str(evidence.get("code") or "")
    row.account_evidence_message = _safe_text(evidence.get("message"))
    row.account_evidence_at = str(evidence.get("at") or "")
    row.probe_state = str(probe.get("state") or "never_checked")
    row.probe_checked_at = str(probe.get("checked_at") or "")
    row.probe_transport = str(probe.get("transport") or "")
    row.probe_error_code = str(probe.get("error_code") or "")
    row.probe_error_message = _safe_text(probe.get("error_message"))
    row.derived_state = str(derived.get("state") or "unknown")
    row.availability_state = str(derived.get("availability") or "unknown")
    row.updated_at = datetime.now(timezone.utc)


def _projection_from_extra_or_account(account: Any, extra: dict[str, Any]) -> dict[str, Any]:
    existing = extra.get(LIFECYCLE_EXTRA_KEY)
    if isinstance(existing, dict) and int(existing.get("schema_version") or 0) >= LIFECYCLE_SCHEMA_VERSION:
        return existing
    return build_account_lifecycle_projection(account, extra)


def apply_probe_lifecycle(
    session: Any,
    account: Any,
    probe: dict[str, Any] | None,
    *,
    extra: dict[str, Any] | None = None,
    operation: str = "local_status_probe",
    probe_id: str = "",
) -> dict[str, Any]:
    """Apply one probe to the durable snapshot and add a redacted evidence event."""

    from core.db import (
        ChatGPTAuthLifecycleModel,
        ChatGPTAuthProbeEventModel,
        ChatGPTSubscriptionStateModel,
    )

    values = extra if isinstance(extra, dict) else (account.get_extra() if hasattr(account, "get_extra") else {})
    if not isinstance(values, dict):
        values = {}
    incoming = probe if isinstance(probe, dict) else {}
    projection = _projection_from_extra_or_account(account, values)
    projection = json.loads(json.dumps(projection, ensure_ascii=False))
    now = str(incoming.get("checked_at") or utc_now_iso())
    auth = incoming.get("auth") if isinstance(incoming.get("auth"), dict) else {}
    refresh_attempt = incoming.get("refresh_attempt") if isinstance(incoming.get("refresh_attempt"), dict) else {}
    access_probe = incoming.get("access_token_probe") if isinstance(incoming.get("access_token_probe"), dict) else auth
    at = projection.setdefault("access_token", {})
    rt = projection.setdefault("refresh_token", {})
    evidence = projection.setdefault("account_evidence", {})
    probe_state = projection.setdefault("probe", {})
    material = projection.setdefault("material", {})
    credentials = _credentials(account, values)
    material.update(
        {
            "has_access_token": bool(credentials["access_token"]),
            "has_refresh_token": bool(credentials["refresh_token"]),
            "has_session_token": bool(credentials["session_token"]),
            "has_cookies": bool(credentials["cookies"]),
            "type": "refresh_token" if credentials["refresh_token"] else "access_token_only" if credentials["access_token"] else "unknown",
        }
    )
    if credentials["access_token"]:
        timing = access_token_timing(account, values, credentials)
        for key in ("issued_at", "expires_at", "expiry_source", "expiry_confidence"):
            if timing.get(key):
                at[key] = timing[key]
        at["observed_at"] = at.get("observed_at") or now
    if refresh_attempt.get("access_token_expires_at"):
        at["expires_at"] = iso_from_value(refresh_attempt.get("access_token_expires_at")) or str(refresh_attempt.get("access_token_expires_at"))
        at["expiry_source"] = "oauth_expires_in"
        at["expiry_confidence"] = "exact"
    refresh_result, refresh_state = classify_refresh_attempt(refresh_attempt)
    if refresh_attempt.get("attempted"):
        rt.update(
            {
                "state": refresh_state,
                "last_attempt_at": now,
                "last_result": refresh_result,
                "last_http_status": int(refresh_attempt.get("http_status") or 0),
                "last_error_code": str(refresh_attempt.get("error_code") or ""),
                "last_error_message": _safe_text(refresh_attempt.get("message")),
            }
        )
        if refresh_attempt.get("success"):
            rt["last_success_at"] = now
        else:
            rt["last_failure_at"] = now
    elif not credentials["refresh_token"]:
        rt.update({"state": "not_present", "last_result": "not_present"})

    access_probe_attempted = access_probe.get("attempted") is not False
    if access_probe_attempted:
        at_state, evidence_state = classify_access_probe(
            access_probe.get("http_status"),
            access_probe.get("error_code"),
            access_probe.get("message"),
        )
    else:
        at_state = "not_present" if not credentials["access_token"] else "unknown"
        if at.get("expires_at") and _as_epoch(at.get("expires_at")) <= _as_epoch(now):
            at_state = "expired"
        evidence_state = "unknown"
    if at_state == "valid":
        at["state"] = "valid"
    elif at_state in {"expired", "revoked", "unauthorized_unknown"}:
        at["state"] = at_state
    elif not credentials["access_token"]:
        at["state"] = "not_present"
    elif at.get("expires_at") and _as_epoch(at.get("expires_at")) <= _as_epoch(now):
        at["state"] = "expired"
    at.update(
        {
            "last_probe_at": now,
            "last_http_status": int(access_probe.get("http_status") or 0),
            "last_error_code": str(access_probe.get("error_code") or ""),
        }
    )
    if evidence_state == "deactivated_confirmed":
        evidence.update(
            {
                "state": evidence_state,
                "code": str(access_probe.get("error_code") or "account_deactivated"),
                "message": _safe_text(access_probe.get("message")),
                "at": now,
            }
        )
    elif evidence_state == "banned_suspected" and evidence.get("state") != "deactivated_confirmed":
        evidence.update(
            {
                "state": evidence_state,
                "code": str(access_probe.get("error_code") or "http_403"),
                "message": _safe_text(access_probe.get("message")),
                "at": now,
            }
        )
    elif evidence_state == "active_confirmed":
        evidence.update({"state": "active_confirmed", "code": "", "message": "", "at": now})

    transport = "network" if int(access_probe.get("http_status") or 0) == 0 else "http"
    probe_state.update(
        {
            "state": (
                "success"
                if access_probe_attempted and at_state in {"valid", "expired", "revoked", "unauthorized_unknown"}
                else "not_checked" if not access_probe_attempted else "failed_transient"
            ),
            "checked_at": now,
            "transport": transport,
            "error_code": str(access_probe.get("error_code") or ""),
            "error_message": _safe_text(access_probe.get("message")),
        }
    )
    derived_state, availability = derive_state(
        at_state=str(at.get("state") or "unknown"),
        rt_state=str(rt.get("state") or "unknown"),
        account_evidence_state=str(evidence.get("state") or "unknown"),
        at_present=bool(material.get("has_access_token")),
        rt_present=bool(material.get("has_refresh_token")),
    )
    projection["derived"] = {"state": derived_state, "availability": availability}
    projection["updated_at"] = utc_now_iso()
    projection["material_revision"] = _material_revision(credentials)
    projection["schema_version"] = LIFECYCLE_SCHEMA_VERSION

    account_id = int(getattr(account, "id", 0) or 0)
    if account_id > 0:
        row = session.get(ChatGPTAuthLifecycleModel, account_id)
        if row is None:
            row = ChatGPTAuthLifecycleModel(account_id=account_id)
        _set_model_from_projection(row, projection)
        session.add(row)

        subscription = incoming.get("subscription") if isinstance(incoming.get("subscription"), dict) else {}
        plan = normalize_plan(subscription.get("plan"))
        subscription_row = session.get(ChatGPTSubscriptionStateModel, account_id)
        if subscription_row is None:
            subscription_row = ChatGPTSubscriptionStateModel(account_id=account_id)
        if plan != "unknown" and at_state == "valid":
            subscription_row.current_plan = plan
            subscription_row.current_active_until = str(subscription.get("subscription_active_until") or "")
            subscription_row.current_checked_at = str(subscription.get("checked_at") or now)
            subscription_row.current_state = "confirmed"
            subscription_row.last_confirmed_plan = plan
            subscription_row.last_confirmed_active_until = subscription_row.current_active_until
            subscription_row.last_confirmed_at = subscription_row.current_checked_at
        elif at_state in {"expired", "revoked", "unauthorized_unknown"}:
            subscription_row.current_plan = "unknown"
            subscription_row.current_active_until = ""
            subscription_row.current_checked_at = now
            subscription_row.current_state = "unconfirmable_auth"
        elif at_state == "probe_failed":
            subscription_row.current_state = "stale_probe_failed"
        subscription_row.workspace_plan_type = str(subscription.get("workspace_plan_type") or subscription_row.workspace_plan_type or "")
        subscription_row.source = str(subscription.get("source") or subscription_row.source or "")
        subscription_row.refresh_state = "confirmed" if plan != "unknown" and at_state == "valid" else subscription_row.current_state
        subscription_row.updated_at = datetime.now(timezone.utc)
        session.add(subscription_row)
        projection["subscription"] = {
            "current_plan": str(subscription_row.current_plan or "unknown"),
            "current_active_until": str(subscription_row.current_active_until or ""),
            "current_checked_at": str(subscription_row.current_checked_at or ""),
            "current_state": str(subscription_row.current_state or "not_checked"),
            "last_confirmed_plan": str(subscription_row.last_confirmed_plan or ""),
            "last_confirmed_active_until": str(subscription_row.last_confirmed_active_until or ""),
            "last_confirmed_at": str(subscription_row.last_confirmed_at or ""),
        }

        event = ChatGPTAuthProbeEventModel(
            account_id=account_id,
            probe_id=probe_id or str(uuid4()),
            material_revision=str(projection.get("material_revision") or ""),
            operation=operation,
            started_at=str(incoming.get("started_at") or now),
            finished_at=now,
            refresh_attempted=bool(refresh_attempt.get("attempted")),
            refresh_result=refresh_result,
            refresh_http_status=int(refresh_attempt.get("http_status") or 0),
            refresh_error_code=str(refresh_attempt.get("error_code") or ""),
            refresh_error_message=_safe_text(refresh_attempt.get("message")),
            access_probe_source=str(access_probe.get("source") or auth.get("source") or ""),
            access_probe_state=at_state,
            access_probe_http_status=int(access_probe.get("http_status") or 0),
            access_probe_error_code=str(access_probe.get("error_code") or ""),
            access_probe_message=_safe_text(access_probe.get("message")),
            account_evidence_state=str(evidence.get("state") or "unknown"),
            account_evidence_code=str(evidence.get("code") or ""),
            subscription_plan=plan,
            subscription_active_until=str(subscription.get("subscription_active_until") or ""),
            payload_json=json.dumps(
                {
                    "auth_state": str(auth.get("state") or ""),
                    "auth_reason": str(auth.get("reason") or auth.get("error_code") or ""),
                    "codex_state": str((incoming.get("codex") or {}).get("state") or "") if isinstance(incoming.get("codex"), dict) else "",
                    "network": incoming.get("network") if isinstance(incoming.get("network"), dict) else {},
                },
                ensure_ascii=False,
            ),
        )
        session.add(event)
    values[LIFECYCLE_EXTRA_KEY] = projection
    return projection


def apply_material_capture(
    session: Any,
    account: Any,
    *,
    extra: dict[str, Any] | None = None,
    access_token_expires_at: Any = "",
    access_token_expiry_source: str = "",
    web_session_expires_at: Any = "",
    operation: str = "material_capture",
) -> dict[str, Any]:
    """Record newly captured material timing without inventing probe evidence."""

    from core.db import (
        ChatGPTAuthLifecycleModel,
        ChatGPTAuthProbeEventModel,
        ChatGPTSubscriptionStateModel,
    )

    values = extra if isinstance(extra, dict) else (account.get_extra() if hasattr(account, "get_extra") else {})
    if not isinstance(values, dict):
        values = {}
    projection = _projection_from_extra_or_account(account, values)
    projection = json.loads(json.dumps(projection, ensure_ascii=False))
    credentials = _credentials(account, values)
    previous_revision = str(projection.get("material_revision") or "")
    next_revision = _material_revision(credentials)
    material_changed = bool(previous_revision and previous_revision != next_revision)
    captured_at = utc_now_iso()
    if material_changed and credentials["access_token"]:
        values["access_token_captured_at"] = captured_at
    timing_values = values if not material_changed else dict(values)
    if material_changed and not access_token_expires_at:
        timing_values.pop("access_token_expires_at", None)
        timing_values.pop("access_token_expiry_source", None)
    if access_token_expires_at:
        timing_values["access_token_expires_at"] = access_token_expires_at
        timing_values["access_token_expiry_source"] = access_token_expiry_source or "oauth_expires_in"
    material = projection.setdefault("material", {})
    material.update(
        {
            "has_access_token": bool(credentials["access_token"]),
            "has_refresh_token": bool(credentials["refresh_token"]),
            "has_session_token": bool(credentials["session_token"]),
            "has_cookies": bool(credentials["cookies"]),
            "type": "refresh_token" if credentials["refresh_token"] else "access_token_only" if credentials["access_token"] else "unknown",
        }
    )
    access = projection.setdefault("access_token", {})
    timing = access_token_timing(
        account,
        timing_values,
        credentials,
        captured_at=captured_at if material_changed else "",
    )
    for key in ("issued_at", "expires_at", "expiry_source", "expiry_confidence"):
        if timing.get(key):
            access[key] = timing[key]
    explicit_access_expiry = iso_from_value(access_token_expires_at)
    if explicit_access_expiry:
        access["expires_at"] = explicit_access_expiry
        access["expiry_source"] = str(access_token_expiry_source or "oauth_expires_in")
        access["expiry_confidence"] = "exact"
    if material_changed:
        access.update(
            {
                "state": "not_present"
                if not credentials["access_token"]
                else "expired"
                if access.get("expires_at") and _as_epoch(access.get("expires_at")) <= _as_epoch(captured_at)
                else "unknown",
                "last_probe_at": "",
                "last_http_status": 0,
                "last_error_code": "",
            }
        )
        refresh = projection.setdefault("refresh_token", {})
        refresh.update(
            {
                "state": "not_attempted" if credentials["refresh_token"] else "not_present",
                "last_attempt_at": "",
                "last_success_at": "",
                "last_failure_at": "",
                "last_result": "not_attempted" if credentials["refresh_token"] else "not_present",
                "last_http_status": 0,
                "last_error_code": "",
                "last_error_message": "",
            }
        )
        projection["account_evidence"] = {
            "state": "unknown",
            "code": "",
            "message": "",
            "at": "",
        }
        projection["probe"] = {
            "state": "never_checked",
            "checked_at": "",
            "transport": "",
            "error_code": "",
            "error_message": "",
        }
        projection["derived"] = {"state": "unknown", "availability": "unknown"}
    if credentials["access_token"]:
        access["observed_at"] = captured_at
        if access.get("expires_at") and _as_epoch(access["expires_at"]) <= _as_epoch(captured_at):
            access["state"] = "expired"
        elif access.get("state") in {"unknown", "not_present"}:
            access["state"] = "unknown"
    web = projection.setdefault("web_session", {})
    explicit_web_expiry = iso_from_value(web_session_expires_at)
    if explicit_web_expiry:
        web.update(
            {
                "expires_at": explicit_web_expiry,
                "expiry_source": "web_session_expires",
                "observed_at": utc_now_iso(),
            }
        )
        values["web_session_expires_at"] = explicit_web_expiry
        values["web_session_expiry_source"] = "web_session_expires"
        values["web_session_observed_at"] = web["observed_at"]
    projection["updated_at"] = captured_at
    projection["material_revision"] = next_revision
    values[LIFECYCLE_EXTRA_KEY] = projection
    account_id = int(getattr(account, "id", 0) or 0)
    if session is not None and account_id > 0:
        row = session.get(ChatGPTAuthLifecycleModel, account_id)
        if row is None:
            row = ChatGPTAuthLifecycleModel(account_id=account_id)
        _set_model_from_projection(row, projection)
        session.add(row)
        subscription_projection = projection.get("subscription") if isinstance(projection.get("subscription"), dict) else {}
        subscription_row = session.get(ChatGPTSubscriptionStateModel, account_id)
        if subscription_row is None:
            subscription_row = ChatGPTSubscriptionStateModel(account_id=account_id)
        if str(subscription_row.current_plan or "unknown") == "unknown":
            subscription_row.current_plan = str(subscription_projection.get("current_plan") or "unknown")
            subscription_row.current_active_until = str(subscription_projection.get("current_active_until") or "")
            subscription_row.current_checked_at = str(subscription_projection.get("current_checked_at") or "")
            subscription_row.current_state = str(subscription_projection.get("current_state") or "not_checked")
        if not subscription_row.last_confirmed_plan:
            subscription_row.last_confirmed_plan = str(subscription_projection.get("last_confirmed_plan") or "")
            subscription_row.last_confirmed_active_until = str(subscription_projection.get("last_confirmed_active_until") or "")
            subscription_row.last_confirmed_at = str(subscription_projection.get("last_confirmed_at") or "")
        session.add(subscription_row)
        session.add(
            ChatGPTAuthProbeEventModel(
                account_id=account_id,
                probe_id=str(uuid4()),
                material_revision=str(projection.get("material_revision") or ""),
                operation=operation,
                started_at=str(projection.get("updated_at") or ""),
                finished_at=str(projection.get("updated_at") or ""),
                access_probe_source="material_capture",
                access_probe_state=str(access.get("state") or "unknown"),
            )
        )
    return projection


def _backfill_lifecycle_rows_batch(engine: Any, account_ids: list[int] | None = None) -> None:
    """Backfill one bounded account batch in its own transaction."""

    from sqlmodel import Session, select
    from core.db import (
        AccountModel,
        ChatGPTAuthLifecycleModel,
        ChatGPTSubscriptionStateModel,
    )

    with Session(engine) as session:
        statement = select(AccountModel).where(AccountModel.platform == "chatgpt")
        if account_ids:
            statement = statement.where(AccountModel.id.in_(account_ids))
        rows = session.exec(statement.order_by(AccountModel.id)).all()
        changed = False
        for account in rows:
            account_id = int(account.id or 0)
            if account_id <= 0:
                continue
            lifecycle = session.get(ChatGPTAuthLifecycleModel, account_id)
            extra = account.get_extra()
            if not isinstance(extra, dict):
                extra = {}
            if lifecycle is not None and int(lifecycle.schema_version or 0) >= LIFECYCLE_SCHEMA_VERSION:
                if not isinstance(extra.get(LIFECYCLE_EXTRA_KEY), dict):
                    extra[LIFECYCLE_EXTRA_KEY] = _model_to_projection(lifecycle)
                    account.set_extra(extra)
                    session.add(account)
                    changed = True
                projection = extra.get(LIFECYCLE_EXTRA_KEY) if isinstance(extra.get(LIFECYCLE_EXTRA_KEY), dict) else _model_to_projection(lifecycle)
                subscription = session.get(ChatGPTSubscriptionStateModel, account_id)
                if subscription is None:
                    subscription = ChatGPTSubscriptionStateModel(account_id=account_id)
                    projection_subscription = projection.get("subscription") if isinstance(projection.get("subscription"), dict) else {}
                    subscription.current_plan = str(projection_subscription.get("current_plan") or "unknown")
                    subscription.current_active_until = str(projection_subscription.get("current_active_until") or "")
                    subscription.current_checked_at = str(projection_subscription.get("current_checked_at") or "")
                    subscription.current_state = str(projection_subscription.get("current_state") or "not_checked")
                    subscription.last_confirmed_plan = str(projection_subscription.get("last_confirmed_plan") or "")
                    subscription.last_confirmed_active_until = str(projection_subscription.get("last_confirmed_active_until") or "")
                    subscription.last_confirmed_at = str(projection_subscription.get("last_confirmed_at") or "")
                    session.add(subscription)
                    changed = True
                continue
            projection = build_account_lifecycle_projection(account, extra)
            if lifecycle is None:
                lifecycle = ChatGPTAuthLifecycleModel(account_id=account_id)
            _set_model_from_projection(lifecycle, projection)
            session.add(lifecycle)
            extra[LIFECYCLE_EXTRA_KEY] = projection
            account.set_extra(extra)
            session.add(account)
            subscription = session.get(ChatGPTSubscriptionStateModel, account_id)
            if subscription is None:
                subscription = ChatGPTSubscriptionStateModel(account_id=account_id)
            historical = extra.get("chatgpt_last_confirmed_subscription")
            if isinstance(historical, dict):
                subscription.last_confirmed_plan = normalize_plan(historical.get("plan"))
                subscription.last_confirmed_active_until = str(
                    historical.get("subscription_active_until")
                    or historical.get("subscription_expires_at_iso")
                    or ""
                )
                subscription.last_confirmed_at = str(historical.get("checked_at") or "")
            session.add(subscription)
            changed = True
        if changed:
            session.commit()


def backfill_existing_lifecycle_rows(engine: Any, *, batch_size: int = 250) -> None:
    """Create lifecycle rows without holding one transaction for the whole account table."""

    from sqlmodel import Session, select
    from core.db import AccountModel

    try:
        size = max(1, int(batch_size or 250))
    except (TypeError, ValueError):
        size = 250
    last_id = 0
    while True:
        with Session(engine) as session:
            ids = session.exec(
                select(AccountModel.id)
                .where(AccountModel.platform == "chatgpt", AccountModel.id > last_id)
                .order_by(AccountModel.id)
                .limit(size)
            ).all()
        account_ids = [int(account_id) for account_id in ids if account_id is not None]
        if not account_ids:
            return
        _backfill_lifecycle_rows_batch(engine, account_ids)
        last_id = max(account_ids)


def lifecycle_from_extra(account: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    values = extra if isinstance(extra, dict) else (account.get_extra() if hasattr(account, "get_extra") else {})
    if not isinstance(values, dict):
        values = {}
    return _projection_from_extra_or_account(account, values)
