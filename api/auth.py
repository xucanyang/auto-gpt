"""Administrator authentication, session revocation, audit and throttling."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json as _json
import os
import re
import secrets
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import case, delete
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from core.db import (
    AdminAuthAuditModel,
    AdminAuthSessionModel,
    AdminAuthThrottleModel,
    engine,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_bearer = HTTPBearer(auto_error=False)
_LEGACY_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_JTI_RE = re.compile(r"^[0-9a-f]{48}$")
_MIN_PASSWORD_LENGTH = 12
_BOOTSTRAP_LOCK = threading.Lock()

_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)


# -- Configuration -----------------------------------------------------------

def _cfg():
    from core.config_store import config_store

    return config_store


def _instance_id() -> str:
    value = str(os.getenv("APP_INSTANCE_ID") or "").strip()
    if not value or not _INSTANCE_ID_RE.fullmatch(value):
        raise HTTPException(status_code=503, detail="管理员认证实例标识未配置")
    return value


def _integer_setting(
    env_name: str,
    config_name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.getenv(env_name)
    if raw is None or not str(raw).strip():
        raw = _cfg().get(config_name, str(default))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _auth_version() -> int:
    try:
        return max(1, int(_cfg().get("auth_version", "1") or "1"))
    except (TypeError, ValueError):
        return 1


def _jwt_secret() -> bytes:
    base_secret = str(os.getenv("APP_JWT_SECRET") or "").strip()
    if not base_secret:
        base_secret = str(_cfg().get("auth_jwt_secret", "") or "").strip()
    if not base_secret:
        base_secret = secrets.token_hex(32)
        _cfg().set("auth_jwt_secret", base_secret)

    # Even a mistakenly copied base secret produces a different signing key
    # for each APP_INSTANCE_ID.
    context = f"auto-gpt-admin-jwt:v2:{_instance_id()}".encode("utf-8")
    return hmac.new(base_secret.encode("utf-8"), context, hashlib.sha256).digest()


def _issuer() -> str:
    return f"auto-gpt-admin:{_instance_id()}"


def _audience() -> str:
    return f"auto-gpt-admin-api:{_instance_id()}"


# -- Request identity --------------------------------------------------------

def _safe_header(value: str, maximum: int) -> str:
    cleaned = "".join(char for char in str(value or "") if char.isprintable())
    return cleaned.strip()[:maximum]


def _parse_ip(value: str):
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _trusted_proxy_networks() -> tuple:
    raw = str(
        os.getenv("APP_TRUSTED_PROXY_CIDRS")
        or os.getenv("AUTH_TRUSTED_PROXY_CIDRS")
        or ""
    ).strip()
    if not raw:
        raw = str(_cfg().get("auth_trusted_proxy_cidrs", "") or "").strip()
    networks = []
    for item in re.split(r"[\s,;]+", raw):
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _is_trusted_proxy(address, networks: tuple) -> bool:
    return bool(address is not None and any(address in network for network in networks))


def _request_metadata(request: Request | None) -> tuple[str, str]:
    if request is None:
        return "system", ""

    peer_text = str(request.client.host if request.client else "unknown")
    peer_ip = _parse_ip(peer_text)
    client_ip = str(peer_ip) if peer_ip is not None else _safe_header(peer_text, 128) or "unknown"
    networks = _trusted_proxy_networks()

    # Forwarded headers are untrusted unless the immediate TCP peer was
    # explicitly allow-listed by CIDR.
    if _is_trusted_proxy(peer_ip, networks):
        cf_ip = _parse_ip(request.headers.get("cf-connecting-ip", ""))
        if cf_ip is not None:
            client_ip = str(cf_ip)
        else:
            forwarded = [
                parsed
                for parsed in (
                    _parse_ip(part) for part in request.headers.get("x-forwarded-for", "").split(",")
                )
                if parsed is not None
            ]
            if forwarded:
                # Walk right-to-left through known proxies and select the first
                # untrusted hop. This rejects a spoofed left-most XFF value.
                selected = None
                for address in reversed(forwarded):
                    if not _is_trusted_proxy(address, networks):
                        selected = address
                        break
                client_ip = str(selected or forwarded[0])
            else:
                real_ip = _parse_ip(request.headers.get("x-real-ip", ""))
                if real_ip is not None:
                    client_ip = str(real_ip)

    user_agent = _safe_header(request.headers.get("user-agent", ""), 512)
    return client_ip, user_agent


def _require_bootstrap_access(request: Request, supplied_token: str) -> None:
    configured_token = str(os.getenv("APP_AUTH_BOOTSTRAP_TOKEN") or "")
    if configured_token:
        if hmac.compare_digest(configured_token, str(supplied_token or "")):
            _reset_rate_limit(request, "bootstrap")
            _record_event(
                request,
                stage="bootstrap",
                outcome="success",
                reason="bootstrap_token_verified",
            )
            return
        _raise_login_failure(
            request,
            stage="bootstrap",
            reason="invalid_bootstrap_token",
            detail="首次初始化凭据无效",
            status_code=403,
        )

    effective_text, _ = _request_metadata(request)
    effective_ip = _parse_ip(effective_text)
    peer_ip = _parse_ip(request.client.host if request.client else "")
    trusted_networks = _trusted_proxy_networks()
    local_source = bool(effective_ip is not None and effective_ip.is_loopback)
    trusted_gateway_source = bool(
        peer_ip is not None
        and effective_ip == peer_ip
        and _is_trusted_proxy(peer_ip, trusted_networks)
        and (peer_ip.is_private or peer_ip.is_loopback or peer_ip.is_link_local)
    )
    if local_source or trusted_gateway_source:
        _reset_rate_limit(request, "bootstrap")
        _record_event(
            request,
            stage="bootstrap",
            outcome="success",
            reason="local_bootstrap_source_verified",
        )
        return
    _raise_login_failure(
        request,
        stage="bootstrap",
        reason="bootstrap_source_not_local",
        detail="首次初始化仅允许本机访问，或配置 APP_AUTH_BOOTSTRAP_TOKEN",
        status_code=403,
    )


# -- Passwords ---------------------------------------------------------------

def _validate_new_password(password: str) -> None:
    if not password or len(password) < _MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"密码至少需要 {_MIN_PASSWORD_LENGTH} 位",
        )
    if len(password) > 1024:
        raise HTTPException(status_code=400, detail="密码长度超出限制")


def _hash_pw(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def _verify_password(password: str, stored: str) -> tuple[bool, bool]:
    """Return (valid, should_migrate_or_rehash)."""
    if len(str(password or "")) > 1024:
        return False, False
    value = str(stored or "").strip()
    if value.startswith("$argon2"):
        try:
            valid = bool(_PASSWORD_HASHER.verify(value, password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False, False
        return valid, bool(valid and _PASSWORD_HASHER.check_needs_rehash(value))
    if _LEGACY_SHA256_RE.fullmatch(value):
        actual = hashlib.sha256(password.encode("utf-8")).hexdigest()
        valid = hmac.compare_digest(actual, value.lower())
        return valid, valid
    return False, False


# -- JWT and server-side sessions -------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _storage_error() -> HTTPException:
    return HTTPException(status_code=503, detail="管理员认证存储暂时不可用")


def _persist_session(
    *,
    jti: str,
    auth_version: int,
    issued_at: int,
    expires_at: int,
    client_ip: str,
    user_agent: str,
) -> None:
    try:
        with Session(engine) as session:
            session.add(
                AdminAuthSessionModel(
                    jti=jti,
                    instance_id=_instance_id(),
                    auth_version=auth_version,
                    issued_at=issued_at,
                    expires_at=expires_at,
                    client_ip=client_ip,
                    user_agent=user_agent,
                )
            )
            session.exec(
                delete(AdminAuthSessionModel).where(
                    AdminAuthSessionModel.instance_id == _instance_id(),
                    AdminAuthSessionModel.expires_at < issued_at - 30 * 86400,
                )
            )
            session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise _storage_error() from exc


def create_token(expire_seconds: int | None = None, request: Request | None = None) -> str:
    now = int(time.time())
    if expire_seconds is None:
        expire_seconds = _integer_setting(
            "AUTH_SESSION_TTL_SECONDS",
            "auth_session_ttl_seconds",
            12 * 3600,
            minimum=300,
            maximum=7 * 86400,
        )
    expire_seconds = max(1, min(7 * 86400, int(expire_seconds)))
    expires_at = now + expire_seconds
    version = _auth_version()
    jti = secrets.token_hex(24)
    client_ip, user_agent = _request_metadata(request)

    header = _b64url_encode(
        _json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8")
    )
    payload_data = {
        "sub": "admin",
        "iss": _issuer(),
        "aud": _audience(),
        "jti": jti,
        "auth_version": version,
        "iat": now,
        "exp": expires_at,
    }
    payload = _b64url_encode(
        _json.dumps(payload_data, separators=(",", ":")).encode("utf-8")
    )
    signature = _b64url_encode(
        hmac.new(_jwt_secret(), f"{header}.{payload}".encode("ascii"), hashlib.sha256).digest()
    )
    _persist_session(
        jti=jti,
        auth_version=version,
        issued_at=now,
        expires_at=expires_at,
        client_ip=client_ip,
        user_agent=user_agent,
    )
    return f"{header}.{payload}.{signature}"


def _unauthorized(detail: str = "无效的令牌") -> HTTPException:
    return HTTPException(status_code=401, detail=detail)


def verify_token(token: str) -> dict:
    try:
        encoded_header, encoded_payload, signature = str(token or "").split(".")
        header = _json.loads(_b64url_decode(encoded_header))
        data = _json.loads(_b64url_decode(encoded_payload))
    except Exception as exc:
        raise _unauthorized("令牌格式错误") from exc

    if header != {"alg": "HS256", "typ": "JWT"}:
        raise _unauthorized()
    expected = _b64url_encode(
        hmac.new(
            _jwt_secret(),
            f"{encoded_header}.{encoded_payload}".encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    if not hmac.compare_digest(signature, expected):
        raise _unauthorized("令牌签名无效")

    now = int(time.time())
    try:
        issued_at = int(data["iat"])
        expires_at = int(data["exp"])
        version = int(data["auth_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _unauthorized("令牌声明不完整") from exc
    jti = str(data.get("jti") or "")
    if (
        data.get("sub") != "admin"
        or data.get("iss") != _issuer()
        or data.get("aud") != _audience()
        or not _JTI_RE.fullmatch(jti)
        or issued_at > now + 60
        or expires_at <= now
        or expires_at <= issued_at
        or version != _auth_version()
    ):
        raise _unauthorized("令牌声明无效或已失效")

    try:
        with Session(engine) as session:
            stored = session.get(AdminAuthSessionModel, jti)
            valid = bool(
                stored
                and stored.instance_id == _instance_id()
                and stored.auth_version == version
                and stored.issued_at == issued_at
                and stored.expires_at == expires_at
                and stored.revoked_at == 0
                and stored.expires_at > now
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise _storage_error() from exc
    if not valid:
        raise _unauthorized("会话已注销或失效")
    return data


def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未认证")
    return verify_token(credentials.credentials)


def _revoke_session(jti: str, reason: str) -> None:
    now = int(time.time())
    try:
        with Session(engine) as session:
            row = session.get(AdminAuthSessionModel, jti)
            if row and row.instance_id == _instance_id() and not row.revoked_at:
                row.revoked_at = now
                row.revoke_reason = _safe_header(reason, 128)
                session.add(row)
                session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise _storage_error() from exc


def _revoke_all_sessions(reason: str) -> int:
    now = int(time.time())
    changed = 0
    try:
        with Session(engine) as session:
            rows = session.exec(
                select(AdminAuthSessionModel).where(
                    AdminAuthSessionModel.instance_id == _instance_id(),
                    AdminAuthSessionModel.revoked_at == 0,
                )
            ).all()
            for row in rows:
                row.revoked_at = now
                row.revoke_reason = _safe_header(reason, 128)
                session.add(row)
                changed += 1
            session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise _storage_error() from exc
    return changed


def _replace_auth_config(values: dict[str, str], reason: str) -> int:
    version = _auth_version() + 1
    data = dict(values)
    data["auth_version"] = str(version)
    try:
        _cfg().set_many(data)
    except Exception as exc:
        raise _storage_error() from exc
    _revoke_all_sessions(reason)
    return version


# -- Persistent audit and throttling ----------------------------------------

def _record_event(
    request: Request | None,
    *,
    stage: str,
    outcome: str,
    reason: str,
    jti: str = "",
) -> None:
    client_ip, user_agent = _request_metadata(request)
    try:
        with Session(engine) as session:
            session.add(
                AdminAuthAuditModel(
                    instance_id=_instance_id(),
                    event_at=int(time.time()),
                    client_ip=client_ip,
                    user_agent=user_agent,
                    stage=_safe_header(stage, 64),
                    outcome=_safe_header(outcome, 32),
                    reason=_safe_header(reason, 128),
                    jti=jti if _JTI_RE.fullmatch(jti) else "",
                )
            )
            session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise _storage_error() from exc


def _throttle_limits(stage: str) -> tuple[int, int, int]:
    is_totp_stage = "totp" in str(stage or "").lower()
    env_prefix = "AUTH_TOTP" if is_totp_stage else "AUTH_PASSWORD"
    config_prefix = "auth_totp" if is_totp_stage else "auth_password"
    maximum = _integer_setting(
        f"{env_prefix}_MAX_FAILURES",
        f"{config_prefix}_max_failures",
        5,
        minimum=2,
        maximum=100,
    )
    window = _integer_setting(
        "AUTH_RATE_LIMIT_WINDOW_SECONDS",
        "auth_rate_limit_window_seconds",
        10 * 60,
        minimum=60,
        maximum=86400,
    )
    cooldown = _integer_setting(
        "AUTH_RATE_LIMIT_COOLDOWN_SECONDS",
        "auth_rate_limit_cooldown_seconds",
        15 * 60,
        minimum=60,
        maximum=86400,
    )
    return maximum, window, cooldown


def _bucket_key(client_ip: str, stage: str) -> str:
    raw = f"{_instance_id()}\0{client_ip}\0{stage}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _enforce_rate_limit(request: Request, stage: str) -> None:
    client_ip, _ = _request_metadata(request)
    now = int(time.time())
    try:
        with Session(engine) as session:
            row = session.get(AdminAuthThrottleModel, _bucket_key(client_ip, stage))
            blocked_until = int(row.blocked_until if row else 0)
    except HTTPException:
        raise
    except Exception as exc:
        raise _storage_error() from exc
    if blocked_until > now:
        _record_event(
            request,
            stage=stage,
            outcome="blocked",
            reason="cooldown_active",
        )
        raise HTTPException(
            status_code=429,
            detail="登录尝试过于频繁，请稍后再试",
            headers={"Retry-After": str(max(1, blocked_until - now))},
        )


def _register_failure(request: Request, stage: str) -> int:
    client_ip, _ = _request_metadata(request)
    maximum, window, cooldown = _throttle_limits(stage)
    now = int(time.time())
    key = _bucket_key(client_ip, stage)
    table = AdminAuthThrottleModel.__table__
    try:
        with Session(engine) as session:
            dialect = session.get_bind().dialect.name
            if dialect in {"sqlite", "postgresql"}:
                insert_factory = sqlite_insert if dialect == "sqlite" else postgresql_insert
                insert_statement = insert_factory(table).values(
                    bucket_key=key,
                    instance_id=_instance_id(),
                    client_ip=client_ip,
                    stage=stage,
                    failure_count=1,
                    window_started_at=now,
                    blocked_until=now + cooldown if maximum <= 1 else 0,
                    updated_at=now,
                )
                reset_window = table.c.window_started_at <= now - window
                next_count = case(
                    (reset_window, 1),
                    else_=table.c.failure_count + 1,
                )
                latest_block = case(
                    (table.c.blocked_until > now + cooldown, table.c.blocked_until),
                    else_=now + cooldown,
                )
                next_blocked_until = case(
                    (next_count >= maximum, latest_block),
                    (reset_window, 0),
                    else_=table.c.blocked_until,
                )
                statement = insert_statement.on_conflict_do_update(
                    index_elements=[table.c.bucket_key],
                    set_={
                        "failure_count": next_count,
                        "window_started_at": case(
                            (reset_window, now),
                            else_=table.c.window_started_at,
                        ),
                        "blocked_until": next_blocked_until,
                        "updated_at": now,
                    },
                ).returning(table.c.blocked_until)
                blocked_until = session.execute(statement).scalar_one()
                session.commit()
                return int(blocked_until or 0)

            # The deployed databases are SQLite/PostgreSQL. Keep an explicit
            # row lock for other SQLAlchemy dialects instead of silently doing
            # an unlocked read-modify-write.
            row = session.exec(
                select(AdminAuthThrottleModel)
                .where(AdminAuthThrottleModel.bucket_key == key)
                .with_for_update()
            ).first()
            if row is None:
                row = AdminAuthThrottleModel(
                    bucket_key=key,
                    instance_id=_instance_id(),
                    client_ip=client_ip,
                    stage=stage,
                    window_started_at=now,
                )
            if not row.window_started_at or now - row.window_started_at >= window:
                row.failure_count = 0
                row.window_started_at = now
                row.blocked_until = 0
            row.failure_count += 1
            row.updated_at = now
            if row.failure_count >= maximum:
                row.blocked_until = max(row.blocked_until, now + cooldown)
            session.add(row)
            session.commit()
            return int(row.blocked_until or 0)
    except HTTPException:
        raise
    except Exception as exc:
        raise _storage_error() from exc


def _reset_rate_limit(request: Request, stage: str) -> None:
    client_ip, _ = _request_metadata(request)
    try:
        with Session(engine) as session:
            session.exec(
                delete(AdminAuthThrottleModel).where(
                    AdminAuthThrottleModel.bucket_key == _bucket_key(client_ip, stage)
                )
            )
            session.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise _storage_error() from exc


def _raise_login_failure(
    request: Request,
    *,
    stage: str,
    reason: str,
    detail: str,
    status_code: int,
) -> None:
    blocked_until = _register_failure(request, stage)
    _record_event(request, stage=stage, outcome="failure", reason=reason)
    now = int(time.time())
    if blocked_until > now:
        raise HTTPException(
            status_code=429,
            detail="登录尝试过于频繁，请稍后再试",
            headers={"Retry-After": str(max(1, blocked_until - now))},
        )
    raise HTTPException(status_code=status_code, detail=detail)


def _require_step_up_password(
    request: Request,
    password: str,
    *,
    jti: str,
) -> None:
    stage = "step_up_password"
    _enforce_rate_limit(request, stage)
    stored = str(_cfg().get("auth_password_hash", "") or "")
    valid, should_rehash = _verify_password(password, stored)
    if not stored or not valid:
        _raise_login_failure(
            request,
            stage=stage,
            reason="invalid_current_password",
            detail="当前密码错误",
            status_code=400,
        )
    if should_rehash:
        try:
            _cfg().set("auth_password_hash", _hash_pw(password))
        except Exception as exc:
            raise _storage_error() from exc
    _reset_rate_limit(request, stage)
    _record_event(
        request,
        stage=stage,
        outcome="success",
        reason="current_password_verified",
        jti=jti,
    )


def _require_step_up_totp(
    request: Request,
    code: str,
    *,
    secret: str,
    jti: str,
) -> None:
    stage = "step_up_totp"
    _enforce_rate_limit(request, stage)
    if not verify_totp(secret, code):
        _raise_login_failure(
            request,
            stage=stage,
            reason="invalid_current_totp",
            detail="当前双因素验证码错误",
            status_code=400,
        )
    _reset_rate_limit(request, stage)
    _record_event(
        request,
        stage=stage,
        outcome="success",
        reason="current_totp_verified",
        jti=jti,
    )


# -- TOTP --------------------------------------------------------------------

def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def totp_uri(secret: str, issuer: str | None = None) -> str:
    from urllib.parse import quote

    issuer = issuer or f"AutoGPT-{_instance_id()}"
    account = quote(f"{issuer}:admin")
    return f"otpauth://totp/{account}?secret={secret}&issuer={quote(issuer)}"


def _totp_at(secret: str, counter: int) -> str:
    try:
        key = base64.b32decode(secret.upper(), casefold=True)
    except Exception:
        return ""
    message = struct.pack(">Q", counter)
    digest = hmac.new(key, message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)


def verify_totp(secret: str, code: str) -> bool:
    counter = int(time.time()) // 30
    user_code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", user_code):
        return False
    return any(
        hmac.compare_digest(_totp_at(secret, counter + delta), user_code)
        for delta in (-1, 0, 1)
    )


@dataclass(frozen=True)
class _PendingTotp:
    instance_id: str
    auth_version: int
    expires_at: float


_pending_2fa: dict[str, _PendingTotp] = {}
_pending_2fa_lock = threading.Lock()


def _clean_pending_totp_unlocked() -> None:
    now = time.time()
    for token, pending in list(_pending_2fa.items()):
        if pending.expires_at <= now:
            _pending_2fa.pop(token, None)


def _clean_pending_totp() -> None:
    with _pending_2fa_lock:
        _clean_pending_totp_unlocked()


# -- Schemas -----------------------------------------------------------------

class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class TotpVerifyRequest(BaseModel):
    temp_token: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=16)


class SetupPasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class EnableTotpRequest(BaseModel):
    secret: str = Field(min_length=16, max_length=128)
    code: str = Field(min_length=1, max_length=16)
    current_password: str = Field(min_length=1, max_length=1024)


class DisableTotpRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    code: str = Field(min_length=1, max_length=16)


# -- Routes ------------------------------------------------------------------

@router.get("/status")
def auth_status():
    password_hash = str(_cfg().get("auth_password_hash", "") or "")
    return {
        "has_password": bool(password_hash),
        "has_totp": bool(_cfg().get("auth_totp_secret", "")),
        "instance_id": _instance_id(),
        "bootstrap_token_required": bool(os.getenv("APP_AUTH_BOOTSTRAP_TOKEN")),
        "min_password_length": _MIN_PASSWORD_LENGTH,
        "password_algorithm": "argon2id" if password_hash.startswith("$argon2id$") else (
            "legacy_sha256" if _LEGACY_SHA256_RE.fullmatch(password_hash) else "unknown"
        ),
    }


@router.post("/setup")
def setup_password(
    body: SetupPasswordRequest,
    request: Request,
    bootstrap_token: str = Header(default="", alias="X-Auth-Bootstrap-Token"),
):
    """Explicit first-run initialization; never a password replacement route."""
    _validate_new_password(body.password)
    _enforce_rate_limit(request, "bootstrap")
    cfg = _cfg()
    with _BOOTSTRAP_LOCK:
        existing = str(cfg.get("auth_password_hash", "") or "")
        if existing:
            _raise_login_failure(
                request,
                stage="bootstrap",
                reason="password_already_initialized",
                detail="管理员密码已设置，请使用修改密码功能",
                status_code=409,
            )
        _require_bootstrap_access(request, bootstrap_token)
        _replace_auth_config({"auth_password_hash": _hash_pw(body.password)}, "password_setup")
    token = create_token(request=request)
    claims = verify_token(token)
    _record_event(
        request,
        stage="credential_change",
        outcome="success",
        reason="password_initialized" if not existing else "password_replaced",
        jti=claims["jti"],
    )
    return {"ok": True, "access_token": token, "token_type": "bearer"}


@router.post("/disable")
def disable_auth(
    request: Request,
    claims: dict = Depends(require_auth),
):
    _record_event(
        request,
        stage="credential_change",
        outcome="failure",
        reason="authentication_disable_forbidden",
        jti=str(claims.get("jti") or ""),
    )
    raise HTTPException(status_code=409, detail="管理员认证不可关闭，请使用修改密码功能")


@router.post("/login")
def login(body: LoginRequest, request: Request):
    _enforce_rate_limit(request, "password")
    cfg = _cfg()
    stored = str(cfg.get("auth_password_hash", "") or "")
    if not stored:
        _raise_login_failure(
            request,
            stage="password",
            reason="authentication_not_initialized",
            detail="no_password_set",
            status_code=403,
        )

    valid, should_rehash = _verify_password(body.password, stored)
    if not valid:
        _raise_login_failure(
            request,
            stage="password",
            reason="invalid_password",
            detail="密码错误",
            status_code=401,
        )
    if should_rehash:
        try:
            cfg.set("auth_password_hash", _hash_pw(body.password))
        except Exception as exc:
            raise _storage_error() from exc

    _reset_rate_limit(request, "password")
    _record_event(
        request,
        stage="password",
        outcome="success",
        reason="password_verified_hash_migrated" if should_rehash else "password_verified",
    )
    totp_secret = str(cfg.get("auth_totp_secret", "") or "")
    if totp_secret:
        with _pending_2fa_lock:
            _clean_pending_totp_unlocked()
            temp_token = secrets.token_hex(24)
            _pending_2fa[temp_token] = _PendingTotp(
                instance_id=_instance_id(),
                auth_version=_auth_version(),
                expires_at=time.time() + 300,
            )
        return {"requires_2fa": True, "temp_token": temp_token}

    token = create_token(request=request)
    claims = verify_token(token)
    _record_event(
        request,
        stage="login",
        outcome="success",
        reason="password_only",
        jti=claims["jti"],
    )
    return {"requires_2fa": False, "access_token": token, "token_type": "bearer"}


@router.post("/verify-totp")
def verify_totp_route(body: TotpVerifyRequest, request: Request):
    _enforce_rate_limit(request, "totp")
    secret = str(_cfg().get("auth_totp_secret", "") or "")
    challenge_state = "invalid"
    with _pending_2fa_lock:
        _clean_pending_totp_unlocked()
        pending = _pending_2fa.get(body.temp_token)
        if (
            pending is not None
            and pending.instance_id == _instance_id()
            and pending.auth_version == _auth_version()
        ):
            if not secret:
                _pending_2fa.pop(body.temp_token, None)
                challenge_state = "totp_disabled"
            elif verify_totp(secret, body.code):
                # Consume before issuing a session so concurrent replays cannot
                # exchange the same password-stage challenge more than once.
                _pending_2fa.pop(body.temp_token, None)
                challenge_state = "accepted"
            else:
                challenge_state = "invalid_code"
        else:
            _pending_2fa.pop(body.temp_token, None)

    if challenge_state == "invalid":
        _raise_login_failure(
            request,
            stage="totp",
            reason="invalid_temp_token",
            detail="临时令牌无效或已过期，请重新登录",
            status_code=401,
        )
    if challenge_state == "totp_disabled":
        _record_event(
            request,
            stage="totp",
            outcome="failure",
            reason="totp_not_enabled",
        )
        raise HTTPException(status_code=400, detail="2FA 未启用")
    if challenge_state == "invalid_code":
        _raise_login_failure(
            request,
            stage="totp",
            reason="invalid_totp",
            detail="验证码错误",
            status_code=400,
        )
    _reset_rate_limit(request, "totp")
    _record_event(
        request,
        stage="totp",
        outcome="success",
        reason="totp_verified",
    )
    token = create_token(request=request)
    claims = verify_token(token)
    _record_event(
        request,
        stage="login",
        outcome="success",
        reason="password_and_totp",
        jti=claims["jti"],
    )
    return {"access_token": token, "token_type": "bearer"}


@router.post("/logout")
def logout(request: Request, claims: dict = Depends(require_auth)):
    jti = str(claims.get("jti") or "")
    _revoke_session(jti, "logout")
    _record_event(
        request,
        stage="logout",
        outcome="success",
        reason="session_revoked",
        jti=jti,
    )
    return {"ok": True}


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    claims: dict = Depends(require_auth),
):
    _validate_new_password(body.new_password)
    _require_step_up_password(
        request,
        body.current_password,
        jti=str(claims.get("jti") or ""),
    )
    _replace_auth_config({"auth_password_hash": _hash_pw(body.new_password)}, "password_changed")
    _record_event(
        request,
        stage="credential_change",
        outcome="success",
        reason="password_changed",
        jti=str(claims.get("jti") or ""),
    )
    return {"ok": True, "reauth_required": True}


@router.get("/2fa/setup")
def setup_2fa(claims: dict = Depends(require_auth)):
    del claims
    secret = generate_totp_secret()
    return {"secret": secret, "uri": totp_uri(secret)}


@router.post("/2fa/enable")
def enable_2fa(
    body: EnableTotpRequest,
    request: Request,
    claims: dict = Depends(require_auth),
):
    if _cfg().get("auth_totp_secret", ""):
        raise HTTPException(status_code=409, detail="双因素认证已启用，请先验证并停用当前配置")
    if (
        not body.secret
        or not 16 <= len(body.secret) <= 128
        or not re.fullmatch(r"[A-Za-z2-7]+=*", body.secret)
    ):
        raise HTTPException(status_code=400, detail="无效的密钥")
    jti = str(claims.get("jti") or "")
    _require_step_up_password(request, body.current_password, jti=jti)
    _enforce_rate_limit(request, "totp_setup")
    if not verify_totp(body.secret, body.code):
        _raise_login_failure(
            request,
            stage="totp_setup",
            reason="invalid_totp_setup_code",
            detail="验证码错误，请重试",
            status_code=400,
        )
    _reset_rate_limit(request, "totp_setup")
    _replace_auth_config({"auth_totp_secret": body.secret}, "totp_enabled")
    _record_event(
        request,
        stage="credential_change",
        outcome="success",
        reason="totp_enabled",
        jti=jti,
    )
    return {"ok": True, "reauth_required": True}


@router.post("/2fa/disable")
def disable_2fa(
    body: DisableTotpRequest,
    request: Request,
    claims: dict = Depends(require_auth),
):
    secret = str(_cfg().get("auth_totp_secret", "") or "")
    if not secret:
        raise HTTPException(status_code=400, detail="双因素认证尚未启用")
    jti = str(claims.get("jti") or "")
    _require_step_up_password(request, body.current_password, jti=jti)
    _require_step_up_totp(request, body.code, secret=secret, jti=jti)
    _replace_auth_config({"auth_totp_secret": ""}, "totp_disabled")
    _record_event(
        request,
        stage="credential_change",
        outcome="success",
        reason="totp_disabled",
        jti=jti,
    )
    return {"ok": True, "reauth_required": True}


@router.get("/sessions")
def list_sessions(
    active_only: bool = True,
    claims: dict = Depends(require_auth),
):
    del claims
    now = int(time.time())
    try:
        with Session(engine) as session:
            statement = select(AdminAuthSessionModel).where(
                AdminAuthSessionModel.instance_id == _instance_id()
            )
            if active_only:
                statement = statement.where(
                    AdminAuthSessionModel.revoked_at == 0,
                    AdminAuthSessionModel.expires_at > now,
                )
            rows = session.exec(statement.order_by(AdminAuthSessionModel.issued_at.desc()).limit(200)).all()
    except HTTPException:
        raise
    except Exception as exc:
        raise _storage_error() from exc
    return {
        "items": [
            {
                "jti": row.jti,
                "issued_at": row.issued_at,
                "expires_at": row.expires_at,
                "revoked_at": row.revoked_at,
                "revoke_reason": row.revoke_reason,
                "client_ip": row.client_ip,
                "user_agent": row.user_agent,
            }
            for row in rows
        ]
    }


@router.post("/sessions/revoke-all")
def revoke_all_sessions(request: Request, claims: dict = Depends(require_auth)):
    count = _revoke_all_sessions("admin_revoke_all")
    _record_event(
        request,
        stage="logout",
        outcome="success",
        reason="all_sessions_revoked",
        jti=str(claims.get("jti") or ""),
    )
    return {"ok": True, "revoked": count, "reauth_required": True}


@router.get("/audit")
def list_auth_audit(
    limit: int = Query(default=100, ge=1, le=500),
    claims: dict = Depends(require_auth),
):
    del claims
    try:
        with Session(engine) as session:
            rows = session.exec(
                select(AdminAuthAuditModel)
                .where(AdminAuthAuditModel.instance_id == _instance_id())
                .order_by(AdminAuthAuditModel.event_at.desc(), AdminAuthAuditModel.id.desc())
                .limit(limit)
            ).all()
    except HTTPException:
        raise
    except Exception as exc:
        raise _storage_error() from exc
    return {
        "items": [
            {
                "id": row.id,
                "instance_id": row.instance_id,
                "event_at": row.event_at,
                "event_time": datetime.fromtimestamp(row.event_at, tz=timezone.utc).isoformat(),
                "client_ip": row.client_ip,
                "user_agent": row.user_agent,
                "stage": row.stage,
                "outcome": row.outcome,
                "reason": row.reason,
                "jti": row.jti,
            }
            for row in rows
        ]
    }
