from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine, select
from starlette.requests import Request

from api import auth
ROOT = Path(__file__).resolve().parents[1]

from core.db import (
    AdminAuthAuditModel,
    AdminAuthSessionModel,
    AdminAuthThrottleModel,
)
from core import db as core_db


class _ConfigStore:
    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def set_many(self, values: dict[str, str]) -> None:
        self.values.update(values)


@pytest.fixture()
def auth_context(tmp_path, monkeypatch):
    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'auth.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(
        test_engine,
        tables=[
            AdminAuthSessionModel.__table__,
            AdminAuthAuditModel.__table__,
            AdminAuthThrottleModel.__table__,
        ],
    )
    store = _ConfigStore({"auth_jwt_secret": "shared-base-secret-for-tests"})
    monkeypatch.setattr(auth, "engine", test_engine)
    monkeypatch.setattr(auth, "_cfg", lambda: store)
    monkeypatch.setenv("APP_INSTANCE_ID", "auto-gpt-plus")
    monkeypatch.delenv("APP_JWT_SECRET", raising=False)
    monkeypatch.delenv("APP_AUTH_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.delenv("APP_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("AUTH_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD_MAX_FAILURES", raising=False)
    monkeypatch.delenv("AUTH_TOTP_MAX_FAILURES", raising=False)
    auth._pending_2fa.clear()

    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    with TestClient(app) as client:
        yield client, store, test_engine
    auth._pending_2fa.clear()


def _authorization(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _legacy_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _request(peer: str, headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (key.lower().encode("latin1"), value.encode("latin1"))
        for key, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/auth/login",
            "raw_path": b"/api/auth/login",
            "query_string": b"",
            "headers": raw_headers,
            "client": (peer, 12345),
            "server": ("testserver", 80),
        }
    )


def _protected_request(headers: dict[str, str] | None = None) -> Request:
    request = _request("127.0.0.1", headers)
    request.scope["path"] = "/api/private"
    request.scope["raw_path"] = b"/api/private"
    return request


def test_legacy_sha256_login_migrates_to_argon2id_and_persists_session(auth_context):
    client, store, test_engine = auth_context
    store.set("auth_password_hash", _legacy_hash("correct-password"))

    response = client.post(
        "/api/auth/login",
        json={"password": "correct-password"},
        headers={"User-Agent": "security-test"},
    )

    assert response.status_code == 200
    token = response.json()["access_token"]
    assert store.get("auth_password_hash").startswith("$argon2id$")
    assert auth._verify_password("correct-password", store.get("auth_password_hash")) == (
        True,
        False,
    )
    claims = auth.verify_token(token)
    assert claims["iss"] == "auto-gpt-admin:auto-gpt-plus"
    assert claims["aud"] == "auto-gpt-admin-api:auto-gpt-plus"
    assert claims["auth_version"] == 1
    assert len(claims["jti"]) == 48

    with Session(test_engine) as session:
        persisted = session.get(AdminAuthSessionModel, claims["jti"])
        events = session.exec(select(AdminAuthAuditModel)).all()
    assert persisted is not None
    assert persisted.instance_id == "auto-gpt-plus"
    assert persisted.user_agent == "security-test"
    assert persisted.last_seen_at == persisted.issued_at
    assert persisted.expires_at == persisted.issued_at + 12 * 60 * 60
    assert persisted.absolute_expires_at == persisted.issued_at + 7 * 24 * 60 * 60
    assert any(event.reason == "password_verified_hash_migrated" for event in events)
    assert any(event.stage == "login" and event.outcome == "success" for event in events)


def test_same_base_secret_cannot_verify_token_in_another_instance(auth_context, monkeypatch):
    _, store, _ = auth_context
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    token = auth.create_token(expire_seconds=600)
    assert auth.verify_token(token)["sub"] == "admin"

    monkeypatch.setenv("APP_INSTANCE_ID", "auto-plus2")
    with pytest.raises(HTTPException) as exc_info:
        auth.verify_token(token)
    assert exc_info.value.status_code == 401


def test_public_auth_status_hides_security_posture_until_authenticated(auth_context):
    client, store, _ = auth_context
    secret = auth.generate_totp_secret()
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    store.set("auth_totp_secret", secret)

    public = client.get("/api/auth/status")
    assert public.status_code == 200
    assert public.json() == {
        "has_password": True,
        "instance_id": "auto-gpt-plus",
        "bootstrap_token_required": False,
        "min_password_length": 12,
    }

    token = auth.create_token(expire_seconds=600)
    authenticated = client.get(
        "/api/auth/status",
        headers=_authorization(token),
    )
    assert authenticated.status_code == 200
    assert authenticated.json()["has_totp"] is True
    assert authenticated.json()["password_algorithm"] == "argon2id"
    assert authenticated.json()["session_idle_timeout_seconds"] == 12 * 60 * 60
    assert authenticated.json()["session_absolute_timeout_seconds"] == 7 * 24 * 60 * 60


def test_active_session_renews_idle_deadline_but_keeps_absolute_deadline(auth_context):
    _, store, test_engine = auth_context
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    token = auth.create_token(request=_request("198.51.100.20", {"User-Agent": "initial"}))
    claims = auth.verify_token(token)
    now = int(time.time())

    with Session(test_engine) as session:
        row = session.get(AdminAuthSessionModel, claims["jti"])
        absolute_deadline = row.absolute_expires_at
        row.expires_at = now + 60
        row.last_seen_at = now - auth._SESSION_ACTIVITY_INTERVAL_SECONDS - 1
        session.add(row)
        session.commit()

    observed = _request("198.51.100.21", {"User-Agent": "renewed-client"})
    auth.verify_token(token, request=observed)

    checked_at = int(time.time())
    with Session(test_engine) as session:
        renewed = session.get(AdminAuthSessionModel, claims["jti"])
    assert checked_at + 12 * 60 * 60 - 2 <= renewed.expires_at <= checked_at + 12 * 60 * 60 + 2
    assert renewed.expires_at <= absolute_deadline
    assert renewed.absolute_expires_at == absolute_deadline == claims["exp"]
    assert renewed.last_seen_at >= checked_at - 2
    assert renewed.client_ip == "198.51.100.21"
    assert renewed.user_agent == "renewed-client"


def test_idle_session_requires_full_reauthentication_even_when_jwt_is_not_expired(auth_context):
    _, store, test_engine = auth_context
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    token = auth.create_token()
    claims = auth.verify_token(token)

    with Session(test_engine) as session:
        row = session.get(AdminAuthSessionModel, claims["jti"])
        assert row.absolute_expires_at > int(time.time())
        row.expires_at = int(time.time()) - 1
        session.add(row)
        session.commit()

    with pytest.raises(HTTPException) as exc_info:
        auth.verify_token(token, request=_request("198.51.100.22"))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "会话已注销或失效"


def test_short_explicit_session_cannot_renew_past_signed_absolute_deadline(auth_context):
    _, store, test_engine = auth_context
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    token = auth.create_token(expire_seconds=600)
    claims = auth.verify_token(token)

    with Session(test_engine) as session:
        row = session.get(AdminAuthSessionModel, claims["jti"])
        row.expires_at = int(time.time()) + 60
        row.last_seen_at = int(time.time()) - auth._SESSION_ACTIVITY_INTERVAL_SECONDS - 1
        session.add(row)
        session.commit()

    auth.verify_token(token, request=_request("198.51.100.23"))
    with Session(test_engine) as session:
        checked = session.get(AdminAuthSessionModel, claims["jti"])
    assert checked.expires_at <= claims["exp"]
    assert checked.absolute_expires_at == claims["exp"]


def test_legacy_session_schema_migration_preserves_fixed_expiry(tmp_path, monkeypatch):
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-auth.db'}")
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE admin_auth_sessions (
                jti TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                auth_version INTEGER NOT NULL,
                issued_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                revoked_at INTEGER NOT NULL,
                revoke_reason TEXT NOT NULL,
                client_ip TEXT NOT NULL,
                user_agent TEXT NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO admin_auth_sessions VALUES "
            "('legacy', 'auto-gpt-plus', 1, 100, 200, 0, '', '', '')"
        )
    monkeypatch.setattr(core_db, "engine", legacy_engine)

    core_db._ensure_admin_auth_session_schema()

    with legacy_engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(admin_auth_sessions)"
            ).fetchall()
        }
        migrated = connection.exec_driver_sql(
            "SELECT last_seen_at, expires_at, absolute_expires_at "
            "FROM admin_auth_sessions WHERE jti = 'legacy'"
        ).one()
    assert {"last_seen_at", "absolute_expires_at"} <= columns
    assert tuple(migrated) == (100, 200, 200)


def test_logout_revokes_current_jti(auth_context):
    client, store, test_engine = auth_context
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    login = client.post("/api/auth/login", json={"password": "correct-password"})
    token = login.json()["access_token"]
    jti = auth.verify_token(token)["jti"]

    response = client.post("/api/auth/logout", headers=_authorization(token))

    assert response.status_code == 200
    with pytest.raises(HTTPException) as exc_info:
        auth.verify_token(token)
    assert exc_info.value.status_code == 401
    with Session(test_engine) as session:
        persisted = session.get(AdminAuthSessionModel, jti)
    assert persisted.revoked_at > 0
    assert persisted.revoke_reason == "logout"


def test_setup_is_initialization_only_and_authentication_cannot_be_disabled(
    auth_context,
    monkeypatch,
):
    client, store, _ = auth_context
    monkeypatch.setenv("APP_AUTH_BOOTSTRAP_TOKEN", "one-time-bootstrap")
    initial = client.post(
        "/api/auth/setup",
        json={"password": "initial-password"},
        headers={"X-Auth-Bootstrap-Token": "one-time-bootstrap"},
    )
    assert initial.status_code == 200
    token = initial.json()["access_token"]
    original_hash = store.get("auth_password_hash")

    replacement = client.post(
        "/api/auth/setup",
        json={"password": "attacker-password"},
        headers=_authorization(token),
    )
    disabled = client.post("/api/auth/disable", headers=_authorization(token))

    assert replacement.status_code == 409
    assert disabled.status_code == 409
    assert store.get("auth_password_hash") == original_hash
    assert auth.verify_token(token)["sub"] == "admin"


def test_bootstrap_requires_token_or_effective_local_source(auth_context, monkeypatch):
    client, _, _ = auth_context
    monkeypatch.setenv("APP_AUTH_BOOTSTRAP_TOKEN", "one-time-bootstrap")
    rejected = client.post("/api/auth/setup", json={"password": "initial-password"})
    assert rejected.status_code == 403
    accepted = client.post(
        "/api/auth/setup",
        json={"password": "initial-password"},
        headers={"X-Auth-Bootstrap-Token": "one-time-bootstrap"},
    )
    assert accepted.status_code == 200


def test_new_password_policy_requires_twelve_characters(auth_context, monkeypatch):
    client, _, _ = auth_context
    monkeypatch.setenv("APP_AUTH_BOOTSTRAP_TOKEN", "one-time-bootstrap")
    response = client.post(
        "/api/auth/setup",
        json={"password": "short-pass"},
        headers={"X-Auth-Bootstrap-Token": "one-time-bootstrap"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "密码至少需要 12 位"


def test_bootstrap_local_fallback_rejects_external_ip_forwarded_by_nginx(
    auth_context,
    monkeypatch,
):
    monkeypatch.delenv("APP_AUTH_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setenv("APP_TRUSTED_PROXY_CIDRS", "172.20.0.1/32")

    auth._require_bootstrap_access(_request("127.0.0.1"), "")
    auth._require_bootstrap_access(_request("172.20.0.1"), "")
    external = _request("172.20.0.1", {"X-Forwarded-For": "198.51.100.8"})
    with pytest.raises(HTTPException) as exc_info:
        auth._require_bootstrap_access(external, "")
    assert exc_info.value.status_code == 403


def test_password_change_bumps_version_and_revokes_all_sessions(auth_context):
    client, store, _ = auth_context
    store.set("auth_password_hash", auth._hash_pw("old-password"))
    first = client.post("/api/auth/login", json={"password": "old-password"}).json()[
        "access_token"
    ]
    second = auth.create_token(expire_seconds=600)

    response = client.post(
        "/api/auth/change-password",
        json={"current_password": "old-password", "new_password": "new-password"},
        headers=_authorization(first),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "reauth_required": True}
    assert store.get("auth_version") == "2"
    assert auth._verify_password("new-password", store.get("auth_password_hash"))[0]
    for token in (first, second):
        with pytest.raises(HTTPException):
            auth.verify_token(token)


def test_totp_change_revokes_sessions_and_requires_reauthentication(auth_context):
    client, store, _ = auth_context
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    token = client.post(
        "/api/auth/login", json={"password": "correct-password"}
    ).json()["access_token"]
    secret = auth.generate_totp_secret()
    code = auth._totp_at(secret, int(time.time()) // 30)

    response = client.post(
        "/api/auth/2fa/enable",
        json={"secret": secret, "code": code, "current_password": "correct-password"},
        headers=_authorization(token),
    )

    assert response.status_code == 200
    assert response.json()["reauth_required"] is True
    assert store.get("auth_totp_secret") == secret
    with pytest.raises(HTTPException):
        auth.verify_token(token)


def test_enabling_totp_rejects_base32_secrets_below_160_bits(auth_context):
    client, store, _ = auth_context
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    token = auth.create_token(expire_seconds=600)
    short_secret = base64.b32encode(b"0123456789").decode("ascii")

    response = client.post(
        "/api/auth/2fa/enable",
        json={
            "secret": short_secret,
            "code": auth._totp_at(short_secret, int(time.time()) // 30),
            "current_password": "correct-password",
        },
        headers=_authorization(token),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "无效或强度不足的密钥"
    assert store.get("auth_totp_secret") == ""


def test_disabling_totp_requires_current_password_and_current_code(auth_context):
    client, store, _ = auth_context
    secret = auth.generate_totp_secret()
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    store.set("auth_totp_secret", secret)
    token = auth.create_token(expire_seconds=600)
    code = auth._totp_at(secret, int(time.time()) // 30)

    rejected = client.post(
        "/api/auth/2fa/disable",
        json={"current_password": "wrong-password", "code": code},
        headers=_authorization(token),
    )
    assert rejected.status_code == 400
    assert store.get("auth_totp_secret") == secret

    accepted = client.post(
        "/api/auth/2fa/disable",
        json={"current_password": "correct-password", "code": code},
        headers=_authorization(token),
    )
    assert accepted.status_code == 200
    assert store.get("auth_totp_secret") == ""
    with pytest.raises(HTTPException):
        auth.verify_token(token)


def test_credential_rotation_invalidates_pending_totp_login(auth_context):
    client, store, _ = auth_context
    secret = auth.generate_totp_secret()
    store.set("auth_password_hash", auth._hash_pw("old-password"))
    store.set("auth_totp_secret", secret)
    pending = client.post(
        "/api/auth/login", json={"password": "old-password"}
    ).json()["temp_token"]
    admin_token = auth.create_token(expire_seconds=600)
    changed = client.post(
        "/api/auth/change-password",
        json={"current_password": "old-password", "new_password": "new-password"},
        headers=_authorization(admin_token),
    )
    assert changed.status_code == 200

    response = client.post(
        "/api/auth/verify-totp",
        json={
            "temp_token": pending,
            "code": auth._totp_at(secret, int(time.time()) // 30),
        },
    )
    assert response.status_code == 401
    assert pending not in auth._pending_2fa



def test_totp_login_is_end_to_end_and_challenge_is_single_use(auth_context):
    client, store, _ = auth_context
    secret = auth.generate_totp_secret()
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    store.set("auth_totp_secret", secret)

    login = client.post("/api/auth/login", json={"password": "correct-password"})
    assert login.status_code == 200
    assert login.json()["requires_2fa"] is True
    temp_token = login.json()["temp_token"]
    payload = {
        "temp_token": temp_token,
        "code": auth._totp_at(secret, int(time.time()) // 30),
    }

    verified = client.post("/api/auth/verify-totp", json=payload)
    replay = client.post("/api/auth/verify-totp", json=payload)

    assert verified.status_code == 200
    assert auth.verify_token(verified.json()["access_token"])["sub"] == "admin"
    assert replay.status_code == 401
    assert temp_token not in auth._pending_2fa


def test_concurrent_totp_challenge_replay_issues_only_one_session(auth_context):
    client, store, _ = auth_context
    secret = auth.generate_totp_secret()
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    store.set("auth_totp_secret", secret)
    temp_token = client.post(
        "/api/auth/login", json={"password": "correct-password"}
    ).json()["temp_token"]
    payload = {
        "temp_token": temp_token,
        "code": auth._totp_at(secret, int(time.time()) // 30),
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda _: client.post("/api/auth/verify-totp", json=payload),
                range(2),
            )
        )

    assert sorted(response.status_code for response in responses) == [200, 401]


def test_totp_challenge_has_a_global_failure_budget_independent_of_ip_throttle(
    auth_context,
    monkeypatch,
):
    client, store, _ = auth_context
    secret = auth.generate_totp_secret()
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    store.set("auth_totp_secret", secret)
    monkeypatch.setenv("AUTH_TOTP_MAX_FAILURES", "100")
    temp_token = client.post(
        "/api/auth/login",
        json={"password": "correct-password"},
        headers={"User-Agent": "trusted-browser"},
    ).json()["temp_token"]
    valid = auth._totp_at(secret, int(time.time()) // 30)
    invalid = "000000" if valid != "000000" else "000001"

    responses = [
        client.post(
            "/api/auth/verify-totp",
            json={"temp_token": temp_token, "code": invalid},
            headers={"User-Agent": "trusted-browser"},
        )
        for _ in range(auth._TOTP_CHALLENGE_MAX_FAILURES)
    ]

    assert [response.status_code for response in responses[:-1]] == [400] * 4
    assert responses[-1].status_code == 401
    assert responses[-1].json()["detail"] == "验证码错误次数过多，请重新输入密码"
    assert temp_token not in auth._pending_2fa
    replay = client.post(
        "/api/auth/verify-totp",
        json={"temp_token": temp_token, "code": valid},
        headers={"User-Agent": "trusted-browser"},
    )
    assert replay.status_code == 401


def test_concurrent_bootstrap_initializes_password_only_once(auth_context, monkeypatch):
    client, store, _ = auth_context
    monkeypatch.setenv("APP_AUTH_BOOTSTRAP_TOKEN", "one-time-bootstrap")

    def setup(password):
        return client.post(
            "/api/auth/setup",
            json={"password": password},
            headers={"X-Auth-Bootstrap-Token": "one-time-bootstrap"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(setup, ("first-password", "second-password")))

    assert sorted(response.status_code for response in responses) == [200, 409]
    stored = store.get("auth_password_hash")
    assert sum(auth._verify_password(value, stored)[0] for value in ("first-password", "second-password")) == 1

def test_password_throttle_and_failure_audit_are_persistent(
    auth_context,
    monkeypatch,
):
    client, store, test_engine = auth_context
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    monkeypatch.setenv("AUTH_PASSWORD_MAX_FAILURES", "2")
    monkeypatch.setenv("AUTH_RATE_LIMIT_COOLDOWN_SECONDS", "60")

    first = client.post("/api/auth/login", json={"password": "wrong-password"})
    second = client.post("/api/auth/login", json={"password": "wrong-password"})
    blocked = client.post("/api/auth/login", json={"password": "correct-password"})

    assert first.status_code == 401
    assert second.status_code == 429
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    with Session(test_engine) as session:
        throttles = session.exec(select(AdminAuthThrottleModel)).all()
        events = session.exec(select(AdminAuthAuditModel)).all()
    assert len(throttles) == 1
    assert throttles[0].failure_count == 2
    assert throttles[0].blocked_until > int(time.time())
    assert sum(event.outcome == "failure" for event in events) == 2
    assert any(event.outcome == "blocked" and event.reason == "cooldown_active" for event in events)


def test_failure_counter_uses_atomic_upsert_under_concurrency(
    auth_context,
    monkeypatch,
):
    _, _, test_engine = auth_context
    monkeypatch.setenv("AUTH_PASSWORD_MAX_FAILURES", "100")
    request = _request("198.51.100.12")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: auth._register_failure(request, "password"),
                range(24),
            )
        )

    assert results == [0] * 24
    with Session(test_engine) as session:
        row = session.exec(select(AdminAuthThrottleModel)).one()
    assert row.failure_count == 24


def test_forwarded_ip_is_ignored_without_explicit_trusted_proxy(auth_context, monkeypatch):
    request = _request(
        "203.0.113.9",
        {"X-Forwarded-For": "198.51.100.8", "User-Agent": "audit-client"},
    )
    assert auth._request_metadata(request) == ("203.0.113.9", "audit-client")

    monkeypatch.setenv("APP_TRUSTED_PROXY_CIDRS", "172.20.0.1/32")
    trusted_request = _request(
        "172.20.0.1",
        {"X-Forwarded-For": "198.51.100.8", "User-Agent": "audit-client"},
    )
    assert auth._request_metadata(trusted_request) == ("198.51.100.8", "audit-client")


def test_missing_instance_id_fails_closed(auth_context, monkeypatch):
    _, store, _ = auth_context
    store.set("auth_password_hash", auth._hash_pw("correct-password"))
    monkeypatch.delenv("APP_INSTANCE_ID", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        auth.create_token(expire_seconds=600)
    assert exc_info.value.status_code == 503


def test_main_middleware_fails_closed_when_password_is_missing(monkeypatch):
    import main
    from core.config_store import config_store

    monkeypatch.setattr(config_store, "get", lambda key, default="": "")
    protected_app = FastAPI()

    @protected_app.middleware("http")
    async def enforce_auth(request, call_next):
        return await main.auth_middleware(request, call_next)

    @protected_app.get("/api/private")
    def private_route():
        return {"unsafe": True}

    with TestClient(protected_app) as client:
        response = client.get("/api/private")
    assert response.status_code == 503
    assert response.json()["detail"] == "管理员认证尚未初始化，请先设置管理员密码"


def test_main_middleware_config_read_does_not_block_event_loop(monkeypatch):
    import main
    from core.config_store import config_store

    started = threading.Event()
    release = threading.Event()

    def slow_config_read(_key, _default=""):
        started.set()
        release.wait(timeout=1)
        return ""

    monkeypatch.setattr(config_store, "get", slow_config_read)

    async def call_next(_request):
        raise AssertionError("protected request must not reach route")

    async def scenario():
        started_at = time.monotonic()
        task = asyncio.create_task(main.auth_middleware(_protected_request(), call_next))
        while not started.is_set() and time.monotonic() - started_at < 0.5:
            await asyncio.sleep(0.005)
        observed_after = time.monotonic() - started_at
        release.set()
        return observed_after, await task

    try:
        observed_after, response = asyncio.run(scenario())
    finally:
        release.set()

    assert observed_after < 0.25
    assert response.status_code == 503


def test_main_middleware_storage_error_fails_closed_with_503(monkeypatch):
    import main
    from core.config_store import config_store

    def failed_config_read(_key, _default=""):
        raise RuntimeError("QueuePool timed out")

    monkeypatch.setattr(config_store, "get", failed_config_read)

    async def call_next(_request):
        raise AssertionError("protected request must not reach route")

    response = asyncio.run(main.auth_middleware(_protected_request(), call_next))
    assert response.status_code == 503
    assert response.body.decode("utf-8").find("认证存储暂时不可用") >= 0


def test_main_middleware_token_verification_does_not_block_event_loop(monkeypatch):
    import main
    from core.config_store import config_store

    started = threading.Event()
    release = threading.Event()

    def slow_verify(_token, _request=None):
        started.set()
        release.wait(timeout=1)
        return {"sub": "admin"}

    monkeypatch.setattr(config_store, "get", lambda _key, _default="": "configured-hash")
    monkeypatch.setattr(auth, "verify_token", slow_verify)

    async def call_next(_request):
        return JSONResponse({"ok": True})

    async def scenario():
        started_at = time.monotonic()
        task = asyncio.create_task(
            main.auth_middleware(
                _protected_request({"Authorization": "Bearer test-token"}),
                call_next,
            )
        )
        while not started.is_set() and time.monotonic() - started_at < 0.5:
            await asyncio.sleep(0.005)
        observed_after = time.monotonic() - started_at
        release.set()
        return observed_after, await task

    try:
        observed_after, response = asyncio.run(scenario())
    finally:
        release.set()

    assert observed_after < 0.25
    assert response.status_code == 200


def test_main_middleware_preserves_401_and_maps_unhandled_verify_failure_to_503(monkeypatch):
    import main
    from core.config_store import config_store

    monkeypatch.setattr(config_store, "get", lambda _key, _default="": "configured-hash")

    async def call_next(_request):
        raise AssertionError("failed auth must not reach route")

    def reject_token(_token, _request=None):
        raise HTTPException(401, "bad token")

    monkeypatch.setattr(auth, "verify_token", reject_token)
    invalid = asyncio.run(
        main.auth_middleware(
            _protected_request({"Authorization": "Bearer invalid-token"}),
            call_next,
        )
    )
    assert invalid.status_code == 401

    def unavailable_storage(_token, _request=None):
        raise RuntimeError("QueuePool timed out")

    monkeypatch.setattr(auth, "verify_token", unavailable_storage)
    unavailable = asyncio.run(
        main.auth_middleware(
            _protected_request({"Authorization": "Bearer valid-shape-token"}),
            call_next,
        )
    )
    assert unavailable.status_code == 503

def test_auth_request_models_bound_credential_field_sizes():
    source = (ROOT / "api" / "auth.py").read_text(encoding="utf-8")
    assert "password: str = Field(min_length=1, max_length=1024)" in source
    assert "temp_token: str = Field(min_length=1, max_length=128)" in source
    assert "code: str = Field(min_length=1, max_length=16)" in source
