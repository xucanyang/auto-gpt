from collections import deque

from services.chatgpt_core.oauth_revoke import (
    CHATGPT_ME_URL,
    OPENAI_OAUTH_REVOKE_URL,
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    revoke_openai_oauth_token,
)


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if isinstance(payload, dict) else {}

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, *, posts=(), gets=()):
        self.posts = deque(posts)
        self.gets = deque(gets)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        value = self.posts.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        value = self.gets.popleft()
        if isinstance(value, Exception):
            raise value
        return value


def test_refresh_token_revoke_matches_official_codex_request_contract():
    session = _FakeSession(posts=[_FakeResponse(200)])

    result = revoke_openai_oauth_token(
        token="rt-secret",
        token_type=TOKEN_TYPE_REFRESH,
        client_id="client-demo",
        session=session,
    )

    assert result.success is True
    assert result.status == "revoked"
    assert result.removable is True
    assert session.calls == [
        (
            "POST",
            OPENAI_OAUTH_REVOKE_URL,
            {
                "headers": {
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                "json": {
                    "token": "rt-secret",
                    "token_type_hint": "refresh_token",
                    "client_id": "client-demo",
                },
                "timeout": 15,
            },
        )
    ]


def test_refresh_token_invalid_response_is_idempotent_success():
    session = _FakeSession(
        posts=[
            _FakeResponse(
                401,
                {
                    "error": {
                        "code": "invalid_refresh_token",
                        "message": "Could not validate your refresh token",
                    }
                },
            )
        ]
    )

    result = revoke_openai_oauth_token(
        token="rt-already-invalid",
        token_type=TOKEN_TYPE_REFRESH,
        session=session,
    )

    assert result.success is True
    assert result.status == "already_invalid"
    assert result.error_code == "invalid_refresh_token"
    assert result.removable is True


def test_access_token_is_removed_only_after_backend_returns_401():
    session = _FakeSession(
        posts=[_FakeResponse(200)],
        gets=[_FakeResponse(401)],
    )

    result = revoke_openai_oauth_token(
        token="at-secret",
        token_type=TOKEN_TYPE_ACCESS,
        session=session,
        verification_delays=(0,),
    )

    assert result.success is True
    assert result.status == "revoked"
    assert result.verification_http_status == 401
    assert result.removable is True
    assert [(method, url) for method, url, _ in session.calls] == [
        ("POST", OPENAI_OAUTH_REVOKE_URL),
        ("GET", CHATGPT_ME_URL),
    ]
    assert session.calls[0][2]["json"] == {
        "token": "at-secret",
        "token_type_hint": "access_token",
    }
    assert session.calls[1][2]["headers"]["authorization"] == "Bearer at-secret"


def test_access_token_invalid_revoke_response_is_not_enough_when_token_still_works():
    session = _FakeSession(
        posts=[
            _FakeResponse(
                400,
                {"error": {"code": "invalid_token", "message": "Invalid token"}},
            )
        ],
        gets=[_FakeResponse(200)],
    )

    result = revoke_openai_oauth_token(
        token="web-at-still-valid",
        token_type=TOKEN_TYPE_ACCESS,
        session=session,
        verification_delays=(0,),
    )

    assert result.success is False
    assert result.status == "failed"
    assert result.error_code == "access_token_still_valid"
    assert result.verification_http_status == 200
    assert result.removable is False


def test_transport_error_is_redacted_and_never_returns_token_material():
    session = _FakeSession(posts=[RuntimeError("request failed with raw at-sensitive")])

    result = revoke_openai_oauth_token(
        token="at-sensitive",
        token_type=TOKEN_TYPE_ACCESS,
        session=session,
        verification_delays=(0,),
    )

    assert result.success is False
    assert result.error_code == "revoke_transport_error"
    assert "at-sensitive" not in result.error_message
    assert "at-sensitive" not in str(result.public_dict())
