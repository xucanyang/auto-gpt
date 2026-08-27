from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace
from unittest import mock

from curl_cffi import requests as cffi_requests

from services.chatgpt_core.token_refresh import (
    BACKEND_ME_URL,
    CHATGPT_HOME_URL,
    TokenRefreshManager,
)


def _jwt(*, account_id: str, expires_in: int = 864000) -> str:
    now = int(time.time())

    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join(
        (
            encode({"alg": "none", "typ": "JWT"}),
            encode(
                {
                    "iat": now,
                    "exp": now + expires_in,
                    "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
                }
            ),
            "signature",
        )
    )


class _Response:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.content = self.text.encode()
        self.headers = {"content-type": "application/json"}
        self.url = "https://chatgpt.com/"

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


def _account(*, account_id: str = "acct-1", access_token: str = "old-at", cookies=None):
    extra = {
        "access_token": access_token,
        "account_id": account_id,
        "session_token": "session-old",
        "chatgpt_browser_cookies": cookies
        or [
            {
                "name": "__Secure-next-auth.session-token",
                "value": "session-old",
                "domain": ".chatgpt.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            }
        ],
    }
    return SimpleNamespace(
        id=1,
        email="account@example.com",
        token=access_token,
        access_token=access_token,
        user_id=account_id,
        session_token="session-old",
        cookies="",
        extra=extra,
    )


def _manager_with_responses(account, responses):
    session = cffi_requests.Session(impersonate="chrome136")
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        response = responses[len(calls) - 1]
        return response

    session.get = get
    manager = TokenRefreshManager()
    manager._create_session = mock.Mock(return_value=session)
    return manager, session, calls


def test_web_session_refresh_rotates_at_and_validates_backend_me():
    account = _account(access_token="old-at")
    fresh = _jwt(account_id="acct-1")
    manager, session, calls = _manager_with_responses(
        account,
        [
            _Response(200, {}),
            _Response(200, {"accessToken": fresh, "sessionToken": "session-new", "account": {"id": "acct-1"}}),
            _Response(200, {"id": "acct-1"}),
        ],
    )

    result = manager.refresh_by_web_session(account)

    assert result.success is True
    assert result.source == "web_session"
    assert result.rotated is True
    assert result.account_id == "acct-1"
    assert result.session_token == "session-new"
    assert result.validation_http_status == 200
    assert result.expires_at is not None
    assert [url for url, _ in calls] == [CHATGPT_HOME_URL, manager.SESSION_URL, BACKEND_ME_URL]
    assert calls[-1][1]["headers"]["Authorization"] == f"Bearer {fresh}"
    assert any(item["name"] == "__Secure-next-auth.session-token" for item in result.structured_cookies or [])
    assert session.cookies.get("__Secure-next-auth.session-token") == "session-old"


def test_web_session_refresh_reports_same_valid_at_without_claiming_rotation():
    account = _account(access_token="same-at")
    manager, _session, _calls = _manager_with_responses(
        account,
        [
            _Response(200, {}),
            _Response(200, {"accessToken": "same-at", "sessionToken": "session-new", "account": {"id": "acct-1"}}),
            _Response(200, {"id": "acct-1"}),
        ],
    )

    result = manager.refresh_by_web_session(account)

    assert result.success is True
    assert result.rotated is False
    assert result.access_token == "same-at"


def test_web_session_refresh_rejects_same_at_when_backend_me_says_invalidated():
    account = _account(access_token="revoked-at")
    manager, _session, calls = _manager_with_responses(
        account,
        [
            _Response(200, {}),
            _Response(200, {"accessToken": "revoked-at", "sessionToken": "session-new", "account": {"id": "acct-1"}}),
            _Response(401, {"error": {"code": "token_invalidated", "message": "invalid"}}),
        ],
    )

    result = manager.refresh_by_web_session(account)

    assert result.success is False
    assert result.error_code == "token_invalidated"
    assert result.validation_http_status == 401
    assert len(calls) == 3


def test_web_session_refresh_rejects_account_identity_mismatch_before_backend_probe():
    account = _account(account_id="acct-local", access_token="old-at")
    manager, _session, calls = _manager_with_responses(
        account,
        [
            _Response(200, {}),
            _Response(200, {"accessToken": "new-at", "sessionToken": "session-new", "account": {"id": "acct-other"}}),
        ],
    )

    result = manager.refresh_by_web_session(account)

    assert result.success is False
    assert result.error_code == "account_identity_mismatch"
    assert len(calls) == 2


def test_web_session_refresh_rejects_backend_identity_mismatch():
    account = _account(account_id="acct-local", access_token="old-at")
    manager, _session, _calls = _manager_with_responses(
        account,
        [
            _Response(200, {}),
            _Response(200, {"accessToken": "new-at", "sessionToken": "session-new", "account": {"id": "acct-local"}}),
            _Response(200, {"account": {"id": "acct-other"}}),
        ],
    )

    result = manager.refresh_by_web_session(account)

    assert result.success is False
    assert result.error_code == "account_identity_mismatch"
    assert result.validation_error_code == "account_identity_mismatch"


def test_web_session_cookie_injection_ignores_untrusted_structured_domains():
    account = _account(
        cookies=[
            {
                "name": "session",
                "value": "trusted",
                "domain": ".chatgpt.com",
                "path": "/",
            },
            {
                "name": "secret",
                "value": "must-not-send",
                "domain": "evil.example",
                "path": "/",
            },
        ]
    )
    manager, session, _calls = _manager_with_responses(
        account,
        [
            _Response(200, {}),
            _Response(200, {"accessToken": "new-at", "sessionToken": "session-new", "account": {"id": "acct-1"}}),
            _Response(200, {"id": "acct-1"}),
        ],
    )

    result = manager.refresh_by_web_session(account)

    assert result.success is True
    assert session.cookies.get("session") == "trusted"
    assert session.cookies.get("secret") is None


def test_refresh_account_auto_falls_back_to_web_session_without_rt():
    account = _account(access_token="old-at")
    manager = TokenRefreshManager()
    expected = mock.Mock(success=True, source="web_session", access_token="new-at")
    with mock.patch.object(manager, "refresh_by_web_session", return_value=expected) as refresh:
        result = manager.refresh_account(account)

    assert result is expected
    refresh.assert_called_once_with(account)


def test_web_session_refresh_does_not_treat_transport_exception_as_success():
    account = _account()
    manager = TokenRefreshManager()
    session = cffi_requests.Session(impersonate="chrome136")
    session.get = mock.Mock(side_effect=TimeoutError("upstream timeout"))
    manager._create_session = mock.Mock(return_value=session)

    result = manager.refresh_by_web_session(account)

    assert result.success is False
    assert result.error_code == "web_session_transport_error"
    assert "upstream timeout" in result.error_message
