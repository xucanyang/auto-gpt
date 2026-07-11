from types import SimpleNamespace

from services.chatgpt_core.web_logout import (
    LOGOUT_DATA_URL,
    SIGNOUT_URL,
    logout_chatgpt_web_session,
)


class _FakeCookies:
    def __init__(self):
        self.values = {}

    @property
    def jar(self):
        return [SimpleNamespace(name=name, value=value) for name, value in self.values.items()]

    def set(self, name, value, domain=None, path=None):
        self.values[name] = value


class _FakeSession:
    def __init__(self, *, preflight_status=200, signout_status=200):
        self.cookies = _FakeCookies()
        self.preflight_status = preflight_status
        self.signout_status = signout_status
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return SimpleNamespace(status_code=self.preflight_status)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return SimpleNamespace(status_code=self.signout_status)


def test_web_logout_replays_captured_cookie_and_csrf_flow_without_oauth_tokens():
    session = _FakeSession()
    result = logout_chatgpt_web_session(
        cookies=(
            "__Secure-next-auth.session-token.0=session-part-a; "
            "__Secure-next-auth.session-token.1=session-part-b; "
            "__Host-next-auth.csrf-token=csrf-value|csrf-hash; oai-did=device-id"
        ),
        proxy_url="http://proxy.example:8080",
        user_agent="Test Browser",
        accept_language="en-US,en;q=0.9",
        session=session,
    )

    assert result.success is True
    assert result.status_code == 200
    assert result.used_session_cookie is True
    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", LOGOUT_DATA_URL),
        ("POST", SIGNOUT_URL),
    ]
    post_kwargs = session.calls[1][2]
    assert post_kwargs["data"] == {
        "csrfToken": "csrf-value",
        "callbackUrl": "https://chatgpt.com/",
        "json": "true",
    }
    assert "authorization" not in {key.lower() for key in post_kwargs["headers"]}
    assert "access_token" not in post_kwargs["data"]
    assert "refresh_token" not in post_kwargs["data"]


def test_web_logout_refuses_missing_csrf_without_sending_request():
    session = _FakeSession()
    result = logout_chatgpt_web_session(
        cookies="__Secure-next-auth.session-token=web-session",
        session=session,
    )

    assert result.success is False
    assert "CSRF" in result.error_message
    assert session.calls == []


def test_web_logout_requires_web_cookie_even_when_no_other_credentials_are_present():
    session = _FakeSession()
    result = logout_chatgpt_web_session(
        cookies="__Host-next-auth.csrf-token=csrf-value|csrf-hash",
        session=session,
    )

    assert result.success is False
    assert "session cookie" in result.error_message
    assert session.calls == []
