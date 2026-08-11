"""Full-fidelity, bounded registration diagnostics.

The browser recorder deliberately combines Playwright Trace and a full HAR:
Trace explains what the automation saw and did, while HAR preserves the HTTP
exchange.  A structured event journal covers backend-only mailbox, proxy and
state-machine activity that neither browser artifact can observe.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid
import zipfile

from sqlmodel import Session, select

from core.timezone import beijing_iso


DIAGNOSTIC_MODE_OFF = "off"
DIAGNOSTIC_MODE_SMART = "smart"
DIAGNOSTIC_MODE_FULL = "full"
DIAGNOSTIC_MODES = frozenset(
    {DIAGNOSTIC_MODE_OFF, DIAGNOSTIC_MODE_SMART, DIAGNOSTIC_MODE_FULL}
)

_CURRENT_SESSION: ContextVar[RegistrationDiagnosticSession | None] = ContextVar(
    "registration_diagnostic_session",
    default=None,
)
_PRUNE_LOCK = threading.Lock()
_KEY_RESPONSE_MARKERS = (
    "/api/accounts/email-otp/send",
    "/api/accounts/email-otp/validate",
    "/api/accounts/user/register",
    "/api/accounts/password/verify",
    "/api/accounts/phone-otp/send",
    "/api/accounts/phone-otp/resend",
    "/api/accounts/phone-otp/validate",
    "/api/accounts/create_account",
    "/api/oauth/oauth2/auth",
    "/api/accounts/consent",
    "/api/accounts/authorize",
    "/api/auth/csrf",
    "/api/auth/signin/openai",
    "/api/auth/session",
    "/backend-api/me",
    "/backend-api/accounts/check/",
    "/error",
)
_CAPTURE_HOST_RE = re.compile(
    r"^https://(?:[^/]+\.)?(?:openai\.com|chatgpt\.com|cloudflare\.com)(?:/|$)",
    re.IGNORECASE,
)
_SECRET_KEY_RE = re.compile(
    r"(?:authorization|password|secret|token|cookie|otp|verification_code|api_key|bearer)",
    re.IGNORECASE,
)
_MAILBOX_MARKERS = (
    "邮箱",
    "验证码",
    "otp",
    "mailbox",
    "tempmail",
    "hme ready",
    "helper",
)
_SAFE_QUERY_KEYS = frozenset(
    {
        "error",
        "error_code",
        "reason",
        "message",
        "type",
        "prompt",
        "screen_hint",
        "response_mode",
    }
)
_DOWNLOADABLE_STATUSES = frozenset({"ready", "truncated", "finalize_failed"})
_VISIBLE_STATUSES = frozenset(
    {"recording", "ready", "truncated", "finalize_failed", "skipped", "pruned"}
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_registration_diagnostics_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "": DIAGNOSTIC_MODE_OFF,
        "0": DIAGNOSTIC_MODE_OFF,
        "false": DIAGNOSTIC_MODE_OFF,
        "disabled": DIAGNOSTIC_MODE_OFF,
        "failure": DIAGNOSTIC_MODE_SMART,
        "failures": DIAGNOSTIC_MODE_SMART,
        "on": DIAGNOSTIC_MODE_SMART,
        "1": DIAGNOSTIC_MODE_SMART,
        "true": DIAGNOSTIC_MODE_SMART,
        "all": DIAGNOSTIC_MODE_FULL,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in DIAGNOSTIC_MODES:
        raise ValueError("注册诊断模式必须是 off、smart 或 full")
    return normalized


def _positive_env(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(str(os.getenv(name) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def diagnostic_limits() -> dict[str, int]:
    gib = 1024**3
    mib = 1024**2
    return {
        "global_bytes": _positive_env(
            "REGISTRATION_DIAGNOSTICS_GLOBAL_MAX_BYTES", 8 * gib
        ),
        "task_bytes": _positive_env(
            "REGISTRATION_DIAGNOSTICS_TASK_MAX_BYTES", 2 * gib
        ),
        "attempt_bytes": _positive_env(
            "REGISTRATION_DIAGNOSTICS_ATTEMPT_MAX_BYTES", 150 * mib
        ),
        "response_bytes": _positive_env(
            "REGISTRATION_DIAGNOSTICS_RESPONSE_MAX_BYTES", 2 * mib
        ),
        "structured_bytes": _positive_env(
            "REGISTRATION_DIAGNOSTICS_STRUCTURED_MAX_BYTES", 20 * mib
        ),
        "reserve_bytes": _positive_env(
            "REGISTRATION_DIAGNOSTICS_FREE_RESERVE_BYTES", 20 * gib
        ),
        "retention_hours": _positive_env(
            "REGISTRATION_DIAGNOSTICS_RETENTION_HOURS", 72
        ),
        "index_retention_hours": _positive_env(
            "REGISTRATION_DIAGNOSTICS_INDEX_RETENTION_HOURS", 720
        ),
        "smart_success_samples": _positive_env(
            "REGISTRATION_DIAGNOSTICS_SUCCESS_SAMPLES", 3
        ),
    }


def diagnostics_root() -> Path:
    runtime = Path(os.getenv("APP_RUNTIME_DIR") or ".").expanduser().resolve()
    root = (runtime / "registration_diagnostics").resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _safe_component(value: Any, *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip())
    return cleaned.strip(".-")[:128] or fallback


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _json_read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str))
        handle.write("\n")
    os.chmod(path, 0o600)


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _directory_file_map(path: Path) -> dict[str, dict[str, int]]:
    if not path.is_dir():
        return {}
    result: dict[str, dict[str, int]] = {}
    for item in sorted(path.iterdir(), key=lambda candidate: candidate.name):
        try:
            if item.is_file() and not item.is_symlink():
                result[item.name] = {"size_bytes": item.stat().st_size}
        except OSError:
            continue
    return result


def _sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists():
        return ""
    for item in sorted(
        (candidate for candidate in path.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(path).as_posix(),
    ):
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        try:
            with item.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            continue
    return digest.hexdigest()


def _redact_text(value: Any) -> str:
    text = str(value or "")
    try:
        from services.chatgpt_core.task_logging import (
            mask_emails_for_log,
            redact_log_text,
        )

        return mask_emails_for_log(redact_log_text(text))
    except Exception:
        text = re.sub(
            r"(?i)(authorization|bearer|password|token|cookie|secret)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            text,
        )
        return text


def _sanitize_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if depth > 7:
        return "[MAX_DEPTH]"
    if key and _SECRET_KEY_RE.search(key):
        if value in (None, "", [], {}):
            return value
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(child_key)[:160]: _sanitize_value(
                child_value,
                key=str(child_key),
                depth=depth + 1,
            )
            for child_key, child_value in list(value.items())[:500]
        }
    if isinstance(value, (list, tuple, set)):
        return [
            _sanitize_value(item, depth=depth + 1)
            for item in list(value)[:500]
        ]
    if isinstance(value, (str, bytes)):
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        return _redact_text(text)[:2_200_000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(value)[:5000]


def _safe_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        query_items = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in _SAFE_QUERY_KEYS:
                query_items.append((key[:80], _redact_text(item)[:500]))
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query_items),
                "",
            )
        )
    except Exception:
        return _redact_text(text.split("#", 1)[0].split("?", 1)[0])[:1000]


def _mask_email(value: Any) -> str:
    text = str(value or "").strip()
    try:
        from services.chatgpt_core.task_logging import mask_email_for_log

        return mask_email_for_log(text)
    except Exception:
        if "@" not in text:
            return text[:3] + "***" if text else ""
        local, domain = text.rsplit("@", 1)
        return f"{local[:2]}***@{domain}"


def _runtime_snapshot() -> dict[str, Any]:
    def read_number(path: str) -> int | None:
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
            return int(text) if text != "max" else None
        except (OSError, ValueError):
            return None

    try:
        load_average = list(os.getloadavg())
    except OSError:
        load_average = []
    return {
        "captured_at": _utcnow().isoformat(),
        "instance_id": str(os.getenv("APP_INSTANCE_ID") or ""),
        "pid": os.getpid(),
        "load_average": load_average,
        "cgroup": {
            "pids_current": read_number("/sys/fs/cgroup/pids.current"),
            "pids_max": read_number("/sys/fs/cgroup/pids.max"),
            "memory_current": read_number("/sys/fs/cgroup/memory.current"),
            "memory_max": read_number("/sys/fs/cgroup/memory.max"),
        },
    }


def _classify_failure(error: str) -> tuple[str, str]:
    text = str(error or "").lower()
    mappings = (
        ("identity_provider_mismatch" in text, "identity_provider_mismatch", "registration_route"),
        ("post_signup_auth_api_failure" in text, "post_signup_auth_api_failure", "post_signup"),
        ("post_signup_navigation_failed" in text, "post_signup_navigation_failed", "post_signup"),
        ("post_signup_duplicate_submission" in text, "post_signup_duplicate_submission", "post_signup"),
        ("post_signup_state_regressed" in text, "post_signup_state_regressed", "post_signup"),
        ("post_signup_state_unresolved" in text, "post_signup_state_unresolved", "post_signup"),
        ("post_signup_existing_account_login_failed" in text, "post_signup_existing_account_login_failed", "web_session"),
        ("post_signup_session_capture_incomplete" in text, "post_signup_session_capture_incomplete", "web_session"),
        ("post_signup_session_capture_failed" in text, "post_signup_session_capture_failed", "web_session"),
        ("session_capture_pending" in text, "session_capture_pending", "web_session"),
        ("authorize" in text and "验证码页" in error, "otp_authorize_reentry", "authorize"),
        ("未支持的注册状态" in error, "unsupported_registration_state", "authorize"),
        ("未获取到验证码" in error, "otp_not_received", "email_otp"),
        ("未收到短信验证码" in error, "sms_otp_not_received", "phone_otp"),
        ("phone-otp/validate" in text, "phone_otp_validate_failed", "phone_otp"),
        ("email-otp/validate" in text, "email_otp_validate_failed", "email_otp"),
        ("user_already_exists" in text, "existing_account", "registration_route"),
        ("invalid_auth_step" in text, "invalid_auth_step", "registration_route"),
        ("invalid_state" in text, "invalid_auth_state", "registration_route"),
        ("about_you" in text or "about-you" in text, "about_you_failed", "about_you"),
        ("incorrect email address or password" in text, "existing_login_password_failed", "password"),
        ("page.goto" in text and "timeout" in text, "browser_navigation_timeout", "navigation"),
        ("web session" in text and ("缺失" in error or "missing" in text), "web_session_incomplete", "web_session"),
        ("accesstoken" in text and ("缺失" in error or "missing" in text), "access_token_missing", "web_session"),
        (("sentinel" in text or "cloudflare" in text or "turnstile" in text) and "403" in text, "antibot_blocked", "sentinel"),
        ("429" in text or "rate limit" in text or "rate_limit" in text, "upstream_rate_limited", "network"),
        ("csrf" in text, "csrf_failed", "authorize"),
        (("page crashed" in text or "browser has been closed" in text or "target closed" in text), "browser_crashed", "browser"),
        ("邮箱页" in error, "email_entry_failed", "email"),
        ("proxy" in text or "socks" in text or "出口 ip" in text, "proxy_failed", "proxy"),
        ("timeout" in text or "超时" in error, "operation_timeout", "unknown"),
    )
    for matched, code, stage in mappings:
        if matched:
            return code, stage
    return ("registration_failed", "unknown") if error else ("", "completed")


def _stage_from_url(value: Any) -> str:
    path = str(value or "").lower()
    if "phone-otp" in path:
        return "phone_otp"
    if "email-otp" in path or "email-verification" in path:
        return "email_otp"
    if "user/register" in path or "create-account/password" in path:
        return "password"
    if "create_account" in path or "about-you" in path:
        return "about_you"
    if "/api/auth/session" in path or "/backend-api/me" in path:
        return "web_session"
    if "authorize" in path or "signin/openai" in path:
        return "authorize"
    return "network"


_STRUCTURED_ERROR_CLASSIFICATIONS = {
    "invalid_username_or_password": ("existing_login_password_failed", "password"),
    "username_already_exists": ("existing_account", "registration_route"),
    "user_already_exists": ("existing_account", "registration_route"),
    "invalid_auth_step": ("invalid_auth_step", "registration_route"),
    "invalid_state": ("invalid_auth_state", "registration_route"),
    "identity_provider_mismatch": ("identity_provider_mismatch", "registration_route"),
}


def _structured_response_error_code(body: Any) -> str:
    text = str(body or "").strip()
    if not text:
        return ""

    candidates: list[Any] = []
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            candidates.extend((error.get("code"), error.get("error_code")))
        elif isinstance(error, str):
            candidates.append(error)
        candidates.extend((payload.get("code"), payload.get("error_code")))

    candidates.extend(
        match.group(1)
        for match in re.finditer(
            r'["\'](?:code|error_code)["\']\s*:\s*["\']([a-z0-9_.-]+)["\']',
            text,
            re.IGNORECASE,
        )
    )
    lowered_text = text.lower()
    candidates.extend(
        code
        for code in _STRUCTURED_ERROR_CLASSIFICATIONS
        if re.search(rf"(?<![a-z0-9_]){re.escape(code)}(?![a-z0-9_])", lowered_text)
    )
    for candidate in candidates:
        normalized = str(candidate or "").strip().lower()
        if normalized in _STRUCTURED_ERROR_CLASSIFICATIONS:
            return normalized
    return ""


def _classify_key_response_failure(
    responses: list[dict[str, Any]],
) -> tuple[str, str]:
    committed_create_account_index: int | None = None
    for index, item in enumerate(responses):
        try:
            status = int(item.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        if (
            200 <= status < 300
            and "/api/accounts/create_account" in str(item.get("url") or "").lower()
        ):
            committed_create_account_index = index
            break

    generic_fallback = ("", "")
    for index in range(len(responses) - 1, -1, -1):
        item = responses[index]
        try:
            status = int(item.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        if status < 400:
            continue
        stage = _stage_from_url(item.get("url"))
        structured_code = _structured_response_error_code(item.get("body"))
        if (
            committed_create_account_index is not None
            and index > committed_create_account_index
            and "/api/accounts/create_account"
            in str(item.get("url") or "").lower()
        ):
            return "post_signup_duplicate_submission", "post_signup"
        if structured_code:
            return _STRUCTURED_ERROR_CLASSIFICATIONS[structured_code]
        if generic_fallback[0]:
            continue
        if status == 403:
            generic_fallback = ("upstream_forbidden", stage)
        elif status == 409:
            generic_fallback = ("upstream_state_conflict", stage)
        elif status == 429:
            generic_fallback = ("upstream_rate_limited", stage)
        elif status >= 500:
            generic_fallback = ("upstream_server_error", stage)
        else:
            generic_fallback = (f"upstream_http_{status}", stage)
    return generic_fallback


def _diagnosis_guidance(failure_code: str, failure_stage: str) -> dict[str, Any]:
    if not str(failure_code or "").strip() and failure_stage == "completed":
        return {
            "kind": "rule_based",
            "title": "注册尝试已完成",
            "stage": "completed",
            "recommended_checks": [],
        }
    guidance = {
        "proxy_failed": (
            "代理或出口链路失败",
            ["核对同一尝试的 request_failed 与出口 IP 事件", "更换候选代理后对比同阶段响应"],
        ),
        "antibot_blocked": (
            "Sentinel 或 Cloudflare 风控拦截",
            ["对照 HAR 中 403 响应与 cf-ray", "比较成功样本的代理、指纹和触发阶段"],
        ),
        "upstream_forbidden": (
            "关键业务接口返回 403",
            ["在 HAR 中定位最后一个 403 业务请求", "核对该请求前的 Sentinel、Cookie 与重定向链"],
        ),
        "upstream_rate_limited": (
            "上游限流",
            ["核对响应头和并发时间线", "降低同出口并发或更换独立出口后对照"],
        ),
        "otp_not_received": (
            "邮箱验证码未到达",
            ["核对 mailbox.jsonl 的发码时间与轮询截止时间", "确认验证码未被旧邮件或重发窗口排除"],
        ),
        "sms_otp_not_received": (
            "短信验证码未到达",
            ["核对发码业务响应与接码 API 时间线", "确认号码状态、重发次数和轮询截止时间"],
        ),
        "email_otp_validate_failed": (
            "邮箱验证码提交失败",
            ["对照 HAR 的 email-otp/validate 响应体", "核对提交码来源与发码时间窗口"],
        ),
        "phone_otp_validate_failed": (
            "短信验证码提交失败",
            ["对照 HAR 的 phone-otp/validate 响应体", "核对接码结果与当前号码会话是否一致"],
        ),
        "existing_account": (
            "注册邮箱已存在",
            ["核对 user/register 的结构化错误码", "确认该邮箱应进入已有账号登录还是永久退役"],
        ),
        "existing_login_password_failed": (
            "已有账号密码登录失败",
            ["核对 password/verify 的结构化错误码", "确认当前注册任务没有复用其他任务的邮箱租约"],
        ),
        "invalid_auth_step": (
            "注册认证步骤已失效",
            ["核对 authorize 前后的重定向链", "对照成功样本确认当前 auth step 与页面状态"],
        ),
        "invalid_auth_state": (
            "注册认证会话状态已失效",
            ["核对最后一个 invalid_state 响应与前序 OTP 请求", "确认没有跨会话复用状态或重复提交"],
        ),
        "identity_provider_mismatch": (
            "邮箱身份提供商与既有账号不匹配",
            ["核对 create_account 的结构化业务码", "将该邮箱永久退出注册候选并确认未重复 signup"],
        ),
        "post_signup_auth_api_failure": (
            "开户后认证回调失败",
            ["确认 create_account 的首个 2xx", "使用已有账号登录恢复 Web Session，禁止重新开户"],
        ),
        "post_signup_navigation_failed": (
            "开户后页面导航失败",
            ["确认 create_account 的首个 2xx", "检查回调 URL 后转已有账号登录恢复"],
        ),
        "post_signup_duplicate_submission": (
            "开户后出现重复提交响应",
            ["以首个 create_account 2xx 为准", "确认后续 invalid_auth_step/invalid_state 未触发重复 signup"],
        ),
        "post_signup_state_regressed": (
            "开户后页面回落到注册阶段",
            ["确认没有再次提交密码、OTP 或 about-you", "改走已有账号登录恢复 Web Session"],
        ),
        "post_signup_state_unresolved": (
            "开户后页面状态无法继续",
            ["确认 create_account 的首个 2xx", "从已开户账号执行登录恢复而非重新注册"],
        ),
        "post_signup_existing_account_login_failed": (
            "开户后已有账号登录恢复失败",
            ["保留 session_capture_pending 账号", "从账号库存重试登录补抓，不再提交 signup"],
        ),
        "post_signup_session_capture_incomplete": (
            "开户后登录成功但 Web Session 仍不完整",
            ["检查 /api/auth/session 与 Cookie 材料", "保留待补抓状态并禁止重复 signup"],
        ),
        "post_signup_session_capture_failed": (
            "开户后 Web Session 抓取异常",
            ["保留开户提交事实和 Cookie 快照", "进入已有账号登录恢复，失败时保存待补抓账号"],
        ),
        "session_capture_pending": (
            "开户完成但 Web Session 待补抓",
            ["从已保存账号执行 existing-account 登录补抓", "确认补抓流程不再提交 create_account"],
        ),
        "web_session_incomplete": (
            "注册后 Web Session 材料不完整",
            ["检查 callback 到 /api/auth/session 的重定向链", "对照最终 Cookie 元数据与成功样本"],
        ),
        "browser_navigation_timeout": (
            "浏览器导航超时",
            ["用 Trace 确认最后一个页面动作", "用 HAR 区分网络等待、业务响应和页面未推进"],
        ),
        "browser_crashed": (
            "浏览器进程或页面异常退出",
            ["核对 runtime.json 的 PID/内存快照", "检查 Trace 尾部和容器资源门控日志"],
        ),
    }
    title, checks = guidance.get(
        str(failure_code or ""),
        (
            "注册尝试未完成",
            ["先查看 diagnosis.json 的最后关键响应", "再用 Trace 与 HAR 对齐失败前后的页面和网络状态"],
        ),
    )
    return {
        "kind": "rule_based",
        "title": title,
        "stage": str(failure_stage or "unknown"),
        "recommended_checks": checks,
    }


def _artifact_model():
    from core.db import RegistrationDiagnosticArtifactModel

    return RegistrationDiagnosticArtifactModel


def _engine():
    from core.db import engine

    return engine


def _row_public(row: Any) -> dict[str, Any]:
    summary = row.get_summary() if hasattr(row, "get_summary") else {}
    files = summary.get("files") if isinstance(summary.get("files"), dict) else {}
    return {
        "id": int(row.id or 0),
        "task_id": str(row.task_id or ""),
        "attempt_id": int(row.attempt_id or 0),
        "attempt_number": int(row.attempt_number or 0),
        "mode": str(row.mode or ""),
        "outcome": str(row.outcome or ""),
        "failure_code": str(row.failure_code or ""),
        "failure_stage": str(row.failure_stage or ""),
        "status": str(row.status or ""),
        "email_masked": str(row.email_masked or ""),
        "size_bytes": int(row.size_bytes or 0),
        "checksum": str(row.checksum or ""),
        "pinned": bool(row.pinned),
        "truncation_reason": str(row.truncation_reason or ""),
        "created_at": beijing_iso(row.created_at),
        "finished_at": beijing_iso(row.finished_at),
        "expires_at": beijing_iso(row.expires_at),
        "summary": summary,
        "files": files,
    }


def list_registration_diagnostics(task_id: str) -> list[dict[str, Any]]:
    Model = _artifact_model()
    with Session(_engine()) as session:
        rows = session.exec(
            select(Model)
            .where(Model.task_id == str(task_id or ""))
            .order_by(Model.created_at.desc())
        ).all()
        return [_row_public(row) for row in rows if row.status in _VISIBLE_STATUSES]


def registration_diagnostics_summary(task_id: str) -> dict[str, Any]:
    items = list_registration_diagnostics(task_id)
    ready = [item for item in items if item["status"] in _DOWNLOADABLE_STATUSES]
    return {
        "artifact_count": len(ready),
        "recording_count": sum(item["status"] == "recording" for item in items),
        "failure_count": sum(item["outcome"] == "failed" for item in ready),
        "success_count": sum(item["outcome"] == "success" for item in ready),
        "total_bytes": sum(int(item["size_bytes"] or 0) for item in ready),
        "latest": ready[0] if ready else None,
    }


def _artifact_row(artifact_id: int, *, task_id: str = ""):
    Model = _artifact_model()
    with Session(_engine()) as session:
        row = session.get(Model, int(artifact_id or 0))
        if row is None or (task_id and row.task_id != task_id):
            raise KeyError("注册诊断包不存在")
        session.expunge(row)
        return row


def _artifact_path(row: Any) -> Path:
    root = diagnostics_root()
    relative = str(row.relative_path or "").strip()
    if not relative:
        raise ValueError("注册诊断包没有可用文件路径")
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("注册诊断包路径越界")
    return candidate


def registration_diagnostic_path(
    artifact_id: int,
    *,
    task_id: str = "",
    filename: str = "",
) -> tuple[Any, Path]:
    row = _artifact_row(artifact_id, task_id=task_id)
    if str(row.status or "") not in _DOWNLOADABLE_STATUSES:
        raise ValueError("注册诊断包尚未完成或已被清理")
    base = _artifact_path(row)
    if filename:
        safe_name = Path(filename).name
        if safe_name != filename or safe_name.startswith("."):
            raise ValueError("诊断文件名无效")
        base = (base / safe_name).resolve()
        artifact_root = _artifact_path(row)
        if artifact_root not in base.parents:
            raise ValueError("诊断文件路径越界")
    if not base.exists() or not base.is_file() and filename:
        raise FileNotFoundError("注册诊断文件不存在")
    return row, base


def build_registration_diagnostic_bundle(
    artifact_id: int,
    *,
    task_id: str = "",
) -> tuple[Any, Path]:
    row = _artifact_row(artifact_id, task_id=task_id)
    if str(row.status or "") not in _DOWNLOADABLE_STATUSES:
        raise ValueError("注册诊断包尚未完成或已被清理")
    source = _artifact_path(row)
    if not source.is_dir():
        raise FileNotFoundError("注册诊断包不存在")
    source_size = max(int(row.size_bytes or 0), _directory_size(source))
    free_bytes = shutil.disk_usage(diagnostics_root()).free
    if free_bytes - source_size < diagnostic_limits()["reserve_bytes"]:
        raise ValueError("磁盘保留空间不足，暂不能生成完整诊断包；可先下载单个制品")
    downloads = diagnostics_root() / ".downloads"
    downloads.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = downloads / (
        f"{_safe_component(row.task_id, fallback='task')}-"
        f"attempt-{int(row.attempt_number or row.attempt_id or 0):04d}-"
        f"{uuid.uuid4().hex[:8]}.zip"
    )
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(source.rglob("*")):
            if item.is_file() and not item.is_symlink():
                archive.write(
                    item,
                    item.relative_to(source).as_posix(),
                    compress_type=(
                        zipfile.ZIP_STORED
                        if item.suffix.lower() in {".zip", ".webm", ".png"}
                        else zipfile.ZIP_DEFLATED
                    ),
                )
    os.chmod(target, 0o600)
    return row, target


def delete_registration_diagnostic(artifact_id: int, *, task_id: str = "") -> dict[str, Any]:
    Model = _artifact_model()
    with Session(_engine()) as session:
        row = session.get(Model, int(artifact_id or 0))
        if row is None or (task_id and row.task_id != task_id):
            raise KeyError("注册诊断包不存在")
        if row.status == "recording":
            raise ValueError("正在采集的诊断包不能删除")
        if row.pinned:
            raise ValueError("固定保留的诊断包必须先取消固定")
        if str(row.relative_path or "").strip():
            path = _artifact_path(row)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        row.status = "deleted"
        row.relative_path = ""
        row.size_bytes = 0
        session.add(row)
        session.commit()
        return _row_public(row)


def set_registration_diagnostic_pinned(
    artifact_id: int,
    *,
    task_id: str = "",
    pinned: bool,
) -> dict[str, Any]:
    Model = _artifact_model()
    with Session(_engine()) as session:
        row = session.get(Model, int(artifact_id or 0))
        if row is None or (task_id and row.task_id != task_id):
            raise KeyError("注册诊断包不存在")
        if str(row.status or "") not in _DOWNLOADABLE_STATUSES:
            raise ValueError("只有可下载的诊断包才能固定保留")
        row.pinned = bool(pinned)
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_public(row)


def _remove_row_payload(
    row: Any,
    *,
    reason: str,
    outcome: str = "",
    status: str = "pruned",
) -> bool:
    Model = _artifact_model()
    with Session(_engine()) as session:
        current = session.get(Model, int(row.id or 0))
        if current is None or current.pinned:
            return False
        if str(current.relative_path or "").strip():
            try:
                path = _artifact_path(current)
            except (ValueError, OSError):
                path = None
            if path is not None and path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        current.status = str(status or "pruned")[:32]
        if outcome:
            current.outcome = str(outcome)[:32]
        current.relative_path = ""
        current.size_bytes = 0
        current.truncation_reason = reason[:1000]
        current.finished_at = current.finished_at or _utcnow()
        session.add(current)
        session.commit()
        return True


def prune_registration_diagnostics(*, task_id: str = "") -> dict[str, Any]:
    if not _PRUNE_LOCK.acquire(blocking=False):
        return {"ok": True, "busy": True, "deleted": 0}
    deleted = 0
    try:
        root = diagnostics_root()
        limits = diagnostic_limits()
        now = _utcnow()
        Model = _artifact_model()
        with Session(_engine()) as session:
            query = select(Model).where(Model.status.in_(tuple(_DOWNLOADABLE_STATUSES)))
            if task_id:
                query = query.where(Model.task_id == task_id)
            rows = list(session.exec(query).all())

        for row in rows:
            expires_at = _as_utc(row.expires_at)
            if not row.pinned and expires_at and expires_at <= now:
                if _remove_row_payload(row, reason="retention_expired"):
                    deleted += 1

        with Session(_engine()) as session:
            query = select(Model).where(Model.status.in_(tuple(_DOWNLOADABLE_STATUSES)))
            if task_id:
                query = query.where(Model.task_id == task_id)
            rows = list(session.exec(query).all())

        grouped: dict[str, list[Any]] = {}
        for row in rows:
            grouped.setdefault(row.task_id, []).append(row)
        for task_rows in grouped.values():
            total = sum(int(row.size_bytes or 0) for row in task_rows)
            candidates = sorted(
                (row for row in task_rows if not row.pinned),
                key=lambda row: (
                    0 if row.outcome == "success" else 1,
                    row.created_at or now,
                ),
            )
            for row in candidates:
                if total <= limits["task_bytes"]:
                    break
                total -= int(row.size_bytes or 0)
                if _remove_row_payload(row, reason="task_quota"):
                    deleted += 1

        with Session(_engine()) as session:
            query = select(Model).where(
                Model.mode == DIAGNOSTIC_MODE_SMART,
                Model.outcome == "success",
                Model.status.in_(tuple(_DOWNLOADABLE_STATUSES)),
            )
            if task_id:
                query = query.where(Model.task_id == task_id)
            smart_success_rows = list(session.exec(query).all())
        smart_groups: dict[str, list[Any]] = {}
        for row in smart_success_rows:
            smart_groups.setdefault(row.task_id, []).append(row)
        sample_limit = limits["smart_success_samples"]
        for task_rows in smart_groups.values():
            unpinned = sorted(
                (row for row in task_rows if not row.pinned),
                key=lambda row: (
                    _as_utc(row.finished_at)
                    or _as_utc(row.created_at)
                    or now
                ),
                reverse=True,
            )
            for row in unpinned[sample_limit:]:
                if _remove_row_payload(
                    row,
                    reason="smart_success_sample_limit",
                    status="discarded",
                ):
                    deleted += 1

        with Session(_engine()) as session:
            rows = list(
                session.exec(
                    select(Model).where(
                        Model.status.in_(tuple(_DOWNLOADABLE_STATUSES))
                    )
                ).all()
            )
        total = sum(int(row.size_bytes or 0) for row in rows)
        free = shutil.disk_usage(root).free
        candidates = sorted(
            (row for row in rows if not row.pinned),
            key=lambda row: (
                0 if row.outcome == "success" else 1,
                row.created_at or now,
            ),
        )
        for row in candidates:
            if total <= limits["global_bytes"] and free >= limits["reserve_bytes"]:
                break
            size = int(row.size_bytes or 0)
            if _remove_row_payload(row, reason="global_quota_or_disk_reserve"):
                total -= size
                free += size
                deleted += 1

        stale_before = now - timedelta(hours=6)
        with Session(_engine()) as session:
            stale_query = select(Model).where(
                Model.status == "recording",
                Model.created_at < stale_before,
            )
            if task_id:
                stale_query = stale_query.where(Model.task_id == task_id)
            stale_rows = list(session.exec(stale_query).all())
        for row in stale_rows:
            if _remove_row_payload(
                row,
                reason="stale_recording_recovered",
                outcome="interrupted",
            ):
                deleted += 1

        cutoff = time.time() - 6 * 3600
        for partial in root.glob("*/.*.partial"):
            try:
                if partial.is_dir() and partial.stat().st_mtime < cutoff:
                    shutil.rmtree(partial, ignore_errors=True)
            except OSError:
                continue

        with Session(_engine()) as session:
            indexed_rows = list(session.exec(select(Model)).all())
            referenced_paths = {
                str(row.relative_path or "").strip()
                for row in indexed_rows
                if str(row.relative_path or "").strip()
            }
        for task_directory in root.iterdir():
            if not task_directory.is_dir() or task_directory.name.startswith("."):
                continue
            for artifact_directory in task_directory.iterdir():
                if (
                    not artifact_directory.is_dir()
                    or artifact_directory.name.startswith(".")
                ):
                    continue
                relative = str(artifact_directory.relative_to(root))
                if relative in referenced_paths:
                    continue
                try:
                    if artifact_directory.stat().st_mtime < cutoff:
                        shutil.rmtree(artifact_directory, ignore_errors=True)
                        deleted += 1
                except OSError:
                    continue

        index_cutoff = now - timedelta(hours=limits["index_retention_hours"])
        with Session(_engine()) as session:
            tombstone_query = select(Model).where(
                Model.status.in_(["deleted", "discarded", "pruned", "skipped"]),
                Model.created_at < index_cutoff,
                Model.pinned == False,  # noqa: E712 - SQLAlchemy expression
            )
            if task_id:
                tombstone_query = tombstone_query.where(Model.task_id == task_id)
            tombstones = list(session.exec(tombstone_query).all())
            for row in tombstones:
                session.delete(row)
            if tombstones:
                session.commit()
        downloads = root / ".downloads"
        if downloads.is_dir():
            for item in downloads.glob("*.zip"):
                try:
                    if item.stat().st_mtime < time.time() - 3600:
                        item.unlink(missing_ok=True)
                except OSError:
                    continue
        return {"ok": True, "busy": False, "deleted": deleted}
    finally:
        _PRUNE_LOCK.release()


class RegistrationDiagnosticSession:
    def __init__(
        self,
        *,
        task_id: str,
        attempt_id: int,
        attempt_number: int,
        mode: str,
        metadata: dict[str, Any] | None = None,
    ):
        self.task_id = str(task_id or "").strip()
        self.attempt_id = int(attempt_id or 0)
        self.attempt_number = int(attempt_number or self.attempt_id or 0)
        self.mode = normalize_registration_diagnostics_mode(mode)
        self.metadata = _sanitize_value(metadata or {})
        self.started_at = _utcnow()
        self._monotonic_start = time.monotonic()
        self._lock = threading.RLock()
        self._token: Token | None = None
        self._finalized = False
        self._finalizing = False
        self._finalize_payload: dict[str, Any] | None = None
        self._final_result: dict[str, Any] | None = None
        self._browser_stopped = False
        self._browser_listeners: list[tuple[Any, str, Any]] = []
        self._warnings: list[str] = []
        self._selected_responses: list[dict[str, Any]] = []
        self._response_budget_used = 0
        self._protocol_har_archive_name = ""
        self._video_unavailable_reason = ""
        self._redirects: list[dict[str, Any]] = []
        self._final_state: dict[str, Any] = {}
        self.root = diagnostics_root()
        task_dir = self.root / _safe_component(self.task_id, fallback="task")
        task_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        stem = f"attempt-{self.attempt_number:04d}-{self.attempt_id}-{uuid.uuid4().hex[:8]}"
        self.partial_dir = task_dir / f".{stem}.partial"
        self.final_dir = task_dir / stem
        self.partial_dir.mkdir(parents=False, exist_ok=False, mode=0o700)
        self.enabled = self.mode != DIAGNOSTIC_MODE_OFF
        limits = diagnostic_limits()
        free = shutil.disk_usage(self.root).free
        if free < limits["reserve_bytes"]:
            self.enabled = False
            self._warnings.append("disk_free_reserve_reached")
        self.artifact_id = self._create_index_row()
        _json_write(
            self.partial_dir / "runtime.json",
            {
                **_runtime_snapshot(),
                "task_id": self.task_id,
                "attempt_id": self.attempt_id,
                "attempt_number": self.attempt_number,
                "mode": self.mode,
                "metadata": self.metadata,
                "capture_enabled": self.enabled,
                "limits": limits,
            },
        )
        self.record_event("diagnostic", "capture_started", {"enabled": self.enabled})

    def _add_warning(self, value: Any) -> None:
        warning = str(value or "").strip()[:1000]
        if not warning:
            return
        with self._lock:
            if warning not in self._warnings:
                self._warnings.append(warning)
                self._warnings[:] = self._warnings[-200:]

    def note_warning(self, value: Any) -> None:
        self._add_warning(value)

    def mark_video_capture_unavailable(self, error: Any) -> None:
        reason = _redact_text(error)[:500] or "runtime_video_capture_unavailable"
        with self._lock:
            self._video_unavailable_reason = reason
        self._add_warning(f"video_capture_unavailable:{reason}")
        shutil.rmtree(self.partial_dir / "video", ignore_errors=True)
        self.record_event(
            "browser",
            "video_capture_unavailable",
            {"reason": reason},
        )

    def _create_index_row(self) -> int:
        Model = _artifact_model()
        with Session(_engine()) as session:
            row = Model(
                task_id=self.task_id,
                attempt_id=self.attempt_id,
                attempt_number=self.attempt_number,
                mode=self.mode,
                outcome="recording",
                status="recording" if self.enabled else "skipped",
                relative_path=str(self.partial_dir.relative_to(self.root)),
                created_at=self.started_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def activate(self) -> None:
        if self._token is None:
            self._token = _CURRENT_SESSION.set(self)

    def record_event(
        self,
        category: str,
        event: str,
        data: dict[str, Any] | None = None,
        *,
        mailbox: bool = False,
    ) -> None:
        if not self.enabled:
            return
        payload = {
            "ts": _utcnow().isoformat(),
            "elapsed_ms": round((time.monotonic() - getattr(self, "_monotonic_start", time.monotonic())) * 1000),
            "category": str(category or "event")[:80],
            "event": str(event or "event")[:160],
            "data": _sanitize_value(data or {}),
        }
        path = self.partial_dir / ("mailbox.jsonl" if mailbox else "events.jsonl")
        try:
            with self._lock:
                _append_jsonl(path, payload)
        except Exception as exc:
            self._add_warning(f"event_write_failed:{type(exc).__name__}")

    def record_log(self, message: Any, level: str = "info") -> None:
        text = _redact_text(message)
        lowered = text.lower()
        self.record_event(
            "task_log",
            "line",
            {"level": str(level or "info"), "message": text[:20_000]},
            mailbox=any(marker in lowered for marker in _MAILBOX_MARKERS),
        )

    def browser_context_options(self) -> dict[str, Any]:
        if not self.enabled:
            return {}
        options: dict[str, Any] = {
            "record_har_path": self.partial_dir / "network.har.zip",
            "record_har_mode": "full",
            "record_har_content": "attach",
            "record_har_url_filter": _CAPTURE_HOST_RE,
        }
        if self.mode == DIAGNOSTIC_MODE_FULL:
            video_dir = self.partial_dir / "video"
            video_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            options["record_video_dir"] = video_dir
        return options

    def record_protocol_http_exchange(
        self,
        *,
        method: Any,
        url: Any,
        request_headers: Any = None,
        request_body: Any = None,
        status: Any = 0,
        response_headers: Any = None,
        response_body: Any = b"",
        duration_ms: Any = 0,
        error: Any = "",
    ) -> None:
        """Append one sanitized curl transaction as a HAR-compatible entry."""

        if not self.enabled:
            return
        try:
            request_header_map = (
                dict(request_headers or {})
                if hasattr(request_headers, "items")
                else {}
            )
            response_header_map = (
                dict(response_headers or {})
                if hasattr(response_headers, "items")
                else {}
            )
            sanitized_request_headers = _sanitize_value(request_header_map)
            sanitized_response_headers = _sanitize_value(response_header_map)
            sanitized_request_body = _sanitize_value(request_body)
            if sanitized_request_body in (None, "", {}, []):
                request_body_text = ""
            elif isinstance(sanitized_request_body, str):
                request_body_text = sanitized_request_body
            else:
                request_body_text = json.dumps(
                    sanitized_request_body,
                    ensure_ascii=False,
                    default=str,
                )
            request_body_text = _redact_text(request_body_text)

            raw_body = (
                response_body
                if isinstance(response_body, bytes)
                else str(response_body or "").encode("utf-8", errors="replace")
            )
            limits = diagnostic_limits()
            with self._lock:
                remaining = max(
                    limits["structured_bytes"] - self._response_budget_used,
                    0,
                )
                maximum = min(limits["response_bytes"], remaining)
                captured = raw_body[:maximum] if maximum > 0 else b""
                self._response_budget_used += len(captured)
            response_text = _redact_text(
                captured.decode("utf-8", errors="replace")
            )
            if len(raw_body) > len(captured):
                response_text += "\n[RESPONSE_TRUNCATED]"

            content_type = str(
                response_header_map.get("content-type")
                or response_header_map.get("Content-Type")
                or ""
            )[:300]
            safe_url = _safe_url(url)
            duration_value = max(float(duration_ms or 0), 0.0)
            safe_query = [
                {"name": key[:80], "value": value[:500]}
                for key, value in parse_qsl(
                    urlsplit(safe_url).query,
                    keep_blank_values=True,
                )
            ]
            if any(marker in str(url or "") for marker in _KEY_RESPONSE_MARKERS):
                key_item = {
                    "ts": _utcnow().isoformat(),
                    "method": str(method or "GET").upper()[:16],
                    "url": safe_url,
                    "status": int(status or 0),
                    "content_type": content_type,
                    "location": _safe_url(
                        response_header_map.get("location")
                        or response_header_map.get("Location")
                        or ""
                    ),
                    "request_id": str(
                        response_header_map.get("x-request-id")
                        or response_header_map.get("X-Request-Id")
                        or response_header_map.get("cf-ray")
                        or response_header_map.get("CF-Ray")
                        or ""
                    )[:300],
                    "body": response_text,
                    "body_truncated": len(raw_body) > len(captured),
                    "transport": "curl_cffi",
                }
                with self._lock:
                    self._selected_responses.append(key_item)
                    self._selected_responses[:] = self._selected_responses[-200:]
                    if key_item["location"]:
                        self._redirects.append(
                            {
                                "status": key_item["status"],
                                "url": safe_url,
                                "location": key_item["location"],
                            }
                        )
                        self._redirects[:] = self._redirects[-100:]
            entry = {
                "startedDateTime": (
                    _utcnow() - timedelta(milliseconds=duration_value)
                ).isoformat(),
                "time": duration_value,
                "request": {
                    "method": str(method or "GET").upper()[:16],
                    "url": safe_url,
                    "httpVersion": "HTTP/2",
                    "cookies": [],
                    "headers": [
                        {"name": str(key)[:160], "value": str(value)[:20_000]}
                        for key, value in dict(sanitized_request_headers or {}).items()
                    ],
                    "queryString": safe_query,
                    "headersSize": -1,
                    "bodySize": len(request_body_text.encode("utf-8", errors="replace")),
                },
                "response": {
                    "status": int(status or 0),
                    "statusText": "",
                    "httpVersion": "HTTP/2",
                    "cookies": [],
                    "headers": [
                        {"name": str(key)[:160], "value": str(value)[:20_000]}
                        for key, value in dict(sanitized_response_headers or {}).items()
                    ],
                    "content": {
                        "size": len(raw_body),
                        "mimeType": content_type,
                        "text": response_text,
                    },
                    "redirectURL": _safe_url(
                        response_header_map.get("location")
                        or response_header_map.get("Location")
                        or ""
                    ),
                    "headersSize": -1,
                    "bodySize": len(raw_body),
                },
                "cache": {},
                "timings": {
                    "blocked": -1,
                    "dns": -1,
                    "connect": -1,
                    "ssl": -1,
                    "send": 0,
                    "wait": duration_value,
                    "receive": 0,
                },
                "_diagnostic": {
                    "sanitized": True,
                    "error": _redact_text(error)[:2000],
                },
            }
            if request_body_text:
                entry["request"]["postData"] = {
                    "mimeType": str(
                        request_header_map.get("content-type")
                        or request_header_map.get("Content-Type")
                        or "application/octet-stream"
                    )[:300],
                    "text": request_body_text[:2_200_000],
                }
            with self._lock:
                _append_jsonl(
                    self.partial_dir / "protocol-har.entries.jsonl",
                    entry,
                )
            self.record_event(
                "network",
                "protocol_response" if not error else "protocol_request_failed",
                {
                    "method": str(method or "GET").upper()[:16],
                    "url": safe_url,
                    "status": int(status or 0),
                    "duration_ms": duration_value,
                    "error": _redact_text(error)[:2000],
                },
            )
        except Exception as exc:
            self._add_warning(f"protocol_har_write_failed:{type(exc).__name__}")

    def _write_protocol_har(self) -> None:
        entries_path = self.partial_dir / "protocol-har.entries.jsonl"
        if not entries_path.is_file():
            return
        har_path = self.partial_dir / "protocol-network.har"
        temporary = har_path.with_name(f".{har_path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8") as target:
            target.write(
                '{"log":{"version":"1.2","creator":'
                '{"name":"auto-gpt-registration-diagnostics","version":"1"},'
                '"pages":[],"entries":['
            )
            first = True
            with entries_path.open("r", encoding="utf-8") as source:
                for line in source:
                    line = line.strip()
                    if not line:
                        continue
                    if not first:
                        target.write(",")
                    target.write(line)
                    first = False
            target.write("]}}")
        os.chmod(temporary, 0o600)
        os.replace(temporary, har_path)
        archive_name = (
            "protocol.har.zip"
            if (self.partial_dir / "network.har.zip").exists()
            else "network.har.zip"
        )
        archive_path = self.partial_dir / archive_name
        with zipfile.ZipFile(
            archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.write(har_path, "network.har")
        os.chmod(archive_path, 0o600)
        self._protocol_har_archive_name = archive_name
        har_path.unlink(missing_ok=True)
        entries_path.unlink(missing_ok=True)

    def start_browser_capture(self, context: Any, page: Any) -> None:
        if not self.enabled:
            return
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except Exception as exc:
            self._add_warning(f"trace_start_failed:{type(exc).__name__}")
        self.record_event("browser", "context_started", {"url": _safe_url(getattr(page, "url", ""))})

        def on_console(message: Any) -> None:
            try:
                payload = {
                    "ts": _utcnow().isoformat(),
                    "type": str(getattr(message, "type", "") or ""),
                    "text": _redact_text(getattr(message, "text", ""))[:20_000],
                    "location": _sanitize_value(getattr(message, "location", None) or {}),
                }
                with self._lock:
                    _append_jsonl(self.partial_dir / "browser-console.jsonl", payload)
            except Exception:
                return

        def on_page_error(error: Any) -> None:
            self.record_event(
                "browser",
                "page_error",
                {"error": _redact_text(error)[:20_000]},
            )

        def on_request_failed(request: Any) -> None:
            try:
                self.record_event(
                    "network",
                    "request_failed",
                    {
                        "method": str(getattr(request, "method", "") or ""),
                        "url": _safe_url(getattr(request, "url", "")),
                        "resource_type": str(getattr(request, "resource_type", "") or ""),
                        "failure": _sanitize_value(getattr(request, "failure", None) or ""),
                    },
                )
            except Exception:
                return

        def on_response(response: Any) -> None:
            try:
                url = str(getattr(response, "url", "") or "")
                if not any(marker in url for marker in _KEY_RESPONSE_MARKERS):
                    return
                request = response.request
                headers = dict(getattr(response, "headers", {}) or {})
                content_type = str(headers.get("content-type") or "")
                body_text = ""
                body_truncated = False
                if any(token in content_type.lower() for token in ("json", "text", "javascript", "html")):
                    try:
                        limits = diagnostic_limits()
                        with self._lock:
                            remaining = max(
                                limits["structured_bytes"] - self._response_budget_used,
                                0,
                            )
                            maximum = min(limits["response_bytes"], remaining)
                        if maximum <= 0:
                            body_truncated = True
                            body_text = "[STRUCTURED_RESPONSE_BUDGET_EXHAUSTED]"
                        else:
                            try:
                                content_length = int(headers.get("content-length") or 0)
                            except (TypeError, ValueError):
                                content_length = 0
                            if content_length > maximum:
                                body_truncated = True
                                body_text = "[BODY_SKIPPED_BY_CONTENT_LENGTH_LIMIT]"
                            else:
                                raw = response.body()
                                body_truncated = len(raw) > maximum
                                captured = raw[:maximum]
                                with self._lock:
                                    self._response_budget_used += len(captured)
                                if captured:
                                    body_text = _redact_text(
                                        captured.decode("utf-8", errors="replace")
                                    )
                    except Exception as exc:
                        body_text = f"[BODY_UNAVAILABLE:{type(exc).__name__}]"
                item = {
                    "ts": _utcnow().isoformat(),
                    "method": str(getattr(request, "method", "") or ""),
                    "url": _safe_url(url),
                    "status": int(getattr(response, "status", 0) or 0),
                    "content_type": content_type[:300],
                    "location": _safe_url(headers.get("location") or ""),
                    "request_id": str(
                        headers.get("x-request-id")
                        or headers.get("cf-ray")
                        or ""
                    )[:300],
                    "body": body_text,
                    "body_truncated": body_truncated,
                }
                with self._lock:
                    self._selected_responses.append(item)
                    self._selected_responses[:] = self._selected_responses[-200:]
                    if 300 <= item["status"] < 400 or item["location"]:
                        self._redirects.append(
                            {
                                "status": item["status"],
                                "url": item["url"],
                                "location": item["location"],
                            }
                        )
                        self._redirects[:] = self._redirects[-100:]
                self.record_event("network", "key_response", item)
            except Exception:
                return

        for event_name, listener in (
            ("console", on_console),
            ("pageerror", on_page_error),
            ("requestfailed", on_request_failed),
            ("response", on_response),
        ):
            try:
                page.on(event_name, listener)
                with self._lock:
                    self._browser_listeners.append((page, event_name, listener))
            except Exception as exc:
                self._add_warning(
                    f"listener_{event_name}_failed:{type(exc).__name__}"
                )

    def stop_browser_capture(self, page: Any, context: Any) -> None:
        with self._lock:
            if self._browser_stopped or not self.enabled:
                return
            self._browser_stopped = True
            listeners = list(self._browser_listeners)
            self._browser_listeners.clear()
        for listener_page, event_name, listener in listeners:
            try:
                listener_page.remove_listener(event_name, listener)
            except Exception:
                continue
        final_state: dict[str, Any] = {
            "captured_at": _utcnow().isoformat(),
            "url": _safe_url(getattr(page, "url", "")),
        }
        try:
            final_state["title"] = _redact_text(page.title())[:1000]
        except Exception as exc:
            final_state["title_error"] = type(exc).__name__
        try:
            html = str(page.content() or "")
            maximum = 5 * 1024 * 1024
            html_bytes = html.encode("utf-8", errors="replace")
            if len(html_bytes) > maximum:
                html = html_bytes[:maximum].decode("utf-8", errors="replace")
                final_state["html_truncated"] = True
            (self.partial_dir / "final-page.html").write_text(html, encoding="utf-8")
            os.chmod(self.partial_dir / "final-page.html", 0o600)
        except Exception as exc:
            self._add_warning(f"html_capture_failed:{type(exc).__name__}")
        try:
            page.screenshot(
                path=self.partial_dir / "final-page.png",
                full_page=True,
                timeout=15_000,
            )
            os.chmod(self.partial_dir / "final-page.png", 0o600)
        except Exception as exc:
            self._add_warning(f"screenshot_failed:{type(exc).__name__}")
        try:
            cookie_items = context.cookies()
            final_state["cookies"] = [
                {
                    "name": str(item.get("name") or ""),
                    "domain": str(item.get("domain") or ""),
                    "path": str(item.get("path") or ""),
                    "expires": item.get("expires"),
                    "http_only": bool(item.get("httpOnly")),
                    "secure": bool(item.get("secure")),
                    "same_site": str(item.get("sameSite") or ""),
                    "value_length": len(str(item.get("value") or "")),
                    "value_sha256": hashlib.sha256(
                        str(item.get("value") or "").encode("utf-8")
                    ).hexdigest(),
                }
                for item in cookie_items
            ]
        except Exception as exc:
            self._add_warning(f"cookie_capture_failed:{type(exc).__name__}")
        with self._lock:
            self._final_state = final_state
        try:
            _json_write(self.partial_dir / "final-state.json", final_state)
        except Exception as exc:
            self._add_warning(f"final_state_write_failed:{type(exc).__name__}")
        try:
            context.tracing.stop(path=self.partial_dir / "trace.zip")
            os.chmod(self.partial_dir / "trace.zip", 0o600)
        except Exception as exc:
            self._add_warning(f"trace_stop_failed:{type(exc).__name__}")

    def _normalize_video(self) -> None:
        video_dir = self.partial_dir / "video"
        if not video_dir.is_dir():
            return
        videos = sorted(video_dir.glob("*.webm"))
        if len(videos) == 1:
            os.replace(videos[0], self.partial_dir / "video.webm")
            os.chmod(self.partial_dir / "video.webm", 0o600)
        elif videos:
            video_archive = self.partial_dir / "video.zip"
            with zipfile.ZipFile(
                video_archive,
                "w",
                compression=zipfile.ZIP_STORED,
            ) as archive:
                for index, video in enumerate(videos, start=1):
                    archive.write(video, f"page-{index}.webm")
            os.chmod(video_archive, 0o600)
        shutil.rmtree(video_dir, ignore_errors=True)

    def _enforce_attempt_limit(self) -> str:
        maximum = diagnostic_limits()["attempt_bytes"]
        size = _directory_size(self.partial_dir)
        removed: list[str] = []
        for filename in (
            "video.webm",
            "video.zip",
            "final-page.html",
            "network.har.zip",
            "protocol.har.zip",
            "trace.zip",
            "browser-console.jsonl",
            "events.jsonl",
            "mailbox.jsonl",
            "protocol-har.entries.jsonl",
        ):
            if size <= maximum:
                break
            path = self.partial_dir / filename
            if not path.exists():
                continue
            try:
                removed.append(filename)
                path.unlink()
                size = _directory_size(self.partial_dir)
            except OSError:
                continue
        return "attempt_quota_removed:" + ",".join(removed) if removed else ""

    def _detach_context(self) -> None:
        if self._token is None:
            return
        try:
            _CURRENT_SESSION.reset(self._token)
        except (LookupError, ValueError):
            pass
        self._token = None

    def _last_response_summary(self, value: Any = None) -> dict[str, Any] | None:
        item = value
        if item is None and self._selected_responses:
            item = self._selected_responses[-1]
        if not isinstance(item, dict):
            return None
        return {
            key: child
            for key, child in item.items()
            if key not in {"body"}
        }

    def _finalize_existing_directory(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        diagnosis = _json_read(self.final_dir / "diagnosis.json")
        manifest = _json_read(self.final_dir / "manifest.json")
        warnings = diagnosis.get("warnings")
        if not isinstance(warnings, list):
            warnings = manifest.get("warnings")
        warnings = [str(item)[:1000] for item in warnings or []]
        quota_reason = next(
            (
                warning
                for warning in warnings
                if warning.startswith("attempt_quota_removed:")
            ),
            "",
        )
        file_map = _directory_file_map(self.final_dir)
        size = _directory_size(self.final_dir)
        checksum = _sha256_tree(self.final_dir)
        final_state = diagnosis.get("final_state")
        final_state = final_state if isinstance(final_state, dict) else {}
        key_responses = diagnosis.get("key_responses")
        last_response = (
            key_responses[-1]
            if isinstance(key_responses, list) and key_responses
            else None
        )
        status = "truncated" if quota_reason else "ready"
        summary = {
            "files": file_map,
            "warnings": warnings,
            "duration_ms": int(diagnosis.get("duration_ms") or 0),
            "final_url": str(final_state.get("url") or ""),
            "last_key_response": self._last_response_summary(last_response),
        }
        self._update_index(
            outcome=payload["outcome"],
            status=status,
            email=payload["email"],
            failure_code=payload["failure_code"],
            failure_stage=payload["failure_stage"],
            summary=summary,
            finished_at=payload["finished_at"],
            size_bytes=size,
            checksum=checksum,
            relative_path=str(self.final_dir.relative_to(self.root)),
            truncation_reason=quota_reason,
        )
        return {
            "artifact_id": self.artifact_id,
            "status": status,
            "outcome": payload["outcome"],
            "failure_code": payload["failure_code"],
            "failure_stage": payload["failure_stage"],
            "size_bytes": size,
        }

    def _finalize_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized_outcome = payload["outcome"]
        failure_code = payload["failure_code"]
        failure_stage = payload["failure_stage"]
        email = payload["email"]
        error = payload["error"]
        finished_at = payload["finished_at"]

        if not self.enabled:
            shutil.rmtree(self.partial_dir, ignore_errors=True)
            self._update_index(
                outcome=normalized_outcome,
                status="skipped",
                email=email,
                failure_code=failure_code,
                failure_stage=failure_stage,
                summary={"warnings": list(self._warnings)},
                finished_at=finished_at,
            )
            return {"artifact_id": self.artifact_id, "status": "skipped"}

        if self.final_dir.is_dir() and not self.partial_dir.exists():
            return self._finalize_existing_directory(payload)

        self._normalize_video()
        try:
            self._write_protocol_har()
        except Exception as exc:
            self._add_warning(f"protocol_har_finalize_failed:{type(exc).__name__}")
        diagnosis = {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "mode": self.mode,
            "outcome": normalized_outcome,
            "failure_code": failure_code,
            "failure_stage": failure_stage,
            "analysis": _diagnosis_guidance(failure_code, failure_stage),
            "error": _redact_text(error)[:20_000],
            "final_state": self._final_state,
            "redirect_chain": self._redirects,
            "key_responses": self._selected_responses,
            "warnings": list(self._warnings),
            "started_at": self.started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_ms": max(
                round((time.monotonic() - self._monotonic_start) * 1000),
                0,
            ),
        }
        _json_write(self.partial_dir / "diagnosis.json", diagnosis)

        quota_reason = self._enforce_attempt_limit()
        if quota_reason:
            self._add_warning(quota_reason)
        diagnosis["warnings"] = list(self._warnings)
        diagnosis["capture"] = {
            "trace": (self.partial_dir / "trace.zip").is_file(),
            "browser_har": (
                (self.partial_dir / "network.har.zip").is_file()
                and self._protocol_har_archive_name != "network.har.zip"
            ),
            "protocol_har": bool(self._protocol_har_archive_name),
            "video": (
                (self.partial_dir / "video.webm").is_file()
                or (self.partial_dir / "video.zip").is_file()
            ),
            "video_requested": self.mode == DIAGNOSTIC_MODE_FULL,
            "video_unavailable_reason": self._video_unavailable_reason,
            "final_dom": (self.partial_dir / "final-page.html").is_file(),
            "final_screenshot": (self.partial_dir / "final-page.png").is_file(),
            "events": (self.partial_dir / "events.jsonl").is_file(),
            "mailbox_events": (self.partial_dir / "mailbox.jsonl").is_file(),
        }
        _json_write(self.partial_dir / "diagnosis.json", diagnosis)

        payload_files = _directory_file_map(self.partial_dir)
        manifest = {
            "schema_version": 1,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "mode": self.mode,
            "outcome": normalized_outcome,
            "failure_code": failure_code,
            "failure_stage": failure_stage,
            "email_masked": _mask_email(email),
            "started_at": self.started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "files": payload_files,
            "warnings": list(self._warnings),
        }
        _json_write(self.partial_dir / "manifest.json", manifest)
        file_map = _directory_file_map(self.partial_dir)
        size = _directory_size(self.partial_dir)
        checksum = _sha256_tree(self.partial_dir)
        os.replace(self.partial_dir, self.final_dir)
        for item in self.final_dir.rglob("*"):
            try:
                os.chmod(item, 0o700 if item.is_dir() else 0o600)
            except OSError:
                continue
        status = "truncated" if quota_reason else "ready"
        summary = {
            "files": file_map,
            "warnings": list(self._warnings),
            "duration_ms": diagnosis["duration_ms"],
            "final_url": str(self._final_state.get("url") or ""),
            "last_key_response": self._last_response_summary(),
        }
        self._update_index(
            outcome=normalized_outcome,
            status=status,
            email=email,
            failure_code=failure_code,
            failure_stage=failure_stage,
            summary=summary,
            finished_at=finished_at,
            size_bytes=size,
            checksum=checksum,
            relative_path=str(self.final_dir.relative_to(self.root)),
            truncation_reason=quota_reason,
        )
        try:
            prune_registration_diagnostics()
        except Exception as exc:
            self._add_warning(f"post_finalize_prune_failed:{type(exc).__name__}")
        return {
            "artifact_id": self.artifact_id,
            "status": status,
            "outcome": normalized_outcome,
            "failure_code": failure_code,
            "failure_stage": failure_stage,
            "size_bytes": size,
        }

    def _mark_finalize_failed(
        self,
        payload: dict[str, Any],
        exc: Exception,
    ) -> None:
        warning = f"finalize_failed:{type(exc).__name__}:{str(exc)[:500]}"
        self._add_warning(warning)
        path = self.final_dir if self.final_dir.is_dir() else self.partial_dir
        relative_path = ""
        size = 0
        checksum = ""
        file_map: dict[str, dict[str, int]] = {}
        if path.is_dir():
            try:
                relative_path = str(path.relative_to(self.root))
                size = _directory_size(path)
                checksum = _sha256_tree(path)
                file_map = _directory_file_map(path)
            except (OSError, ValueError):
                relative_path = ""
        try:
            self._update_index(
                outcome=payload["outcome"],
                status="finalize_failed",
                email=payload["email"],
                failure_code=payload["failure_code"],
                failure_stage=payload["failure_stage"],
                summary={"files": file_map, "warnings": list(self._warnings)},
                finished_at=payload["finished_at"],
                size_bytes=size,
                checksum=checksum,
                relative_path=relative_path,
                truncation_reason=warning,
            )
        except Exception:
            pass

    def finalize(
        self,
        *,
        outcome: str,
        error: str = "",
        email: str = "",
        reason_code: str = "",
    ) -> dict[str, Any]:
        normalized_outcome = str(outcome or "failed").strip().lower()
        if normalized_outcome == "success":
            failure_code, failure_stage = "", "completed"
        else:
            failure_code, failure_stage = _classify_failure(error)
            if failure_code in {"", "registration_failed"}:
                response_code, response_stage = _classify_key_response_failure(
                    self._selected_responses
                )
                if response_code:
                    failure_code, failure_stage = response_code, response_stage
            normalized_reason = str(reason_code or "").strip()[:96]
            if normalized_reason:
                generic_reasons = {"registration_failed", "failed", "error", "skipped"}
                if failure_code == "registration_failed" or normalized_reason not in generic_reasons:
                    failure_code = normalized_reason
        with self._lock:
            if self._finalized:
                return dict(
                    self._final_result
                    or {"artifact_id": self.artifact_id, "status": "already_finalized"}
                )
            if self._finalizing:
                return {"artifact_id": self.artifact_id, "status": "finalizing"}
            if self._finalize_payload is None:
                self._finalize_payload = {
                    "outcome": normalized_outcome,
                    "error": str(error or ""),
                    "email": str(email or ""),
                    "failure_code": failure_code,
                    "failure_stage": failure_stage,
                    "finished_at": _utcnow(),
                }
            payload = dict(self._finalize_payload)
            self._finalizing = True
        self._detach_context()
        try:
            result = self._finalize_once(payload)
        except Exception as exc:
            self._mark_finalize_failed(payload, exc)
            raise
        else:
            with self._lock:
                self._finalized = True
                self._final_result = dict(result)
            return result
        finally:
            with self._lock:
                self._finalizing = False

    def _update_index(
        self,
        *,
        outcome: str,
        status: str,
        email: str,
        failure_code: str,
        failure_stage: str,
        summary: dict[str, Any],
        finished_at: datetime,
        size_bytes: int = 0,
        checksum: str = "",
        relative_path: str = "",
        truncation_reason: str = "",
    ) -> None:
        Model = _artifact_model()
        with Session(_engine()) as session:
            row = session.get(Model, self.artifact_id)
            if row is None:
                return
            row.outcome = str(outcome or "")[:32]
            row.status = str(status or "")[:32]
            row.email_masked = _mask_email(email)
            row.failure_code = str(failure_code or "")[:96]
            row.failure_stage = str(failure_stage or "")[:64]
            row.relative_path = str(relative_path or "")[:512]
            row.size_bytes = max(int(size_bytes or 0), 0)
            row.checksum = str(checksum or "")[:64]
            row.truncation_reason = str(truncation_reason or "")[:1000]
            row.finished_at = finished_at
            row.expires_at = finished_at + timedelta(
                hours=diagnostic_limits()["retention_hours"]
            )
            row.set_summary(summary)
            session.add(row)
            session.commit()


def create_registration_diagnostic_session(
    *,
    task_id: str,
    attempt_id: int,
    attempt_number: int,
    mode: str,
    metadata: dict[str, Any] | None = None,
) -> RegistrationDiagnosticSession | None:
    normalized = normalize_registration_diagnostics_mode(mode)
    if normalized == DIAGNOSTIC_MODE_OFF:
        return None
    try:
        prune_registration_diagnostics()
    except Exception:
        pass
    session = RegistrationDiagnosticSession(
        task_id=task_id,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        mode=normalized,
        metadata=metadata,
    )
    session.activate()
    return session


def current_registration_diagnostic_session() -> RegistrationDiagnosticSession | None:
    return _CURRENT_SESSION.get()


def record_registration_diagnostic_event(
    category: str,
    event: str,
    data: dict[str, Any] | None = None,
    *,
    mailbox: bool = False,
) -> None:
    session = current_registration_diagnostic_session()
    if session is not None:
        try:
            session.record_event(category, event, data, mailbox=mailbox)
        except Exception as exc:
            session._add_warning(f"event_capture_failed:{type(exc).__name__}")


def record_registration_diagnostic_log(message: Any, level: str = "info") -> None:
    session = current_registration_diagnostic_session()
    if session is not None:
        try:
            session.record_log(message, level)
        except Exception as exc:
            session._add_warning(f"log_capture_failed:{type(exc).__name__}")


def record_registration_protocol_http_exchange(**payload: Any) -> None:
    session = current_registration_diagnostic_session()
    if session is not None:
        try:
            session.record_protocol_http_exchange(**payload)
        except Exception as exc:
            session._add_warning(f"protocol_capture_failed:{type(exc).__name__}")


def diagnostic_files(item: dict[str, Any]) -> Iterable[str]:
    files = item.get("files") if isinstance(item.get("files"), dict) else {}
    return tuple(str(name) for name in files)
