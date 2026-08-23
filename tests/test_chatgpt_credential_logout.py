from unittest import mock

from api.actions import _apply_action_result
from core.base_platform import Account, AccountStatus, RegisterConfig
from core.db import AccountModel
from services.chatgpt_core.auth_lifecycle import (
    LIFECYCLE_EXTRA_KEY,
    build_account_lifecycle_projection,
)
from services.chatgpt_core.credential_logout import (
    ACCESS_TOKEN_KEYS,
    ID_TOKEN_KEYS,
    REFRESH_TOKEN_KEYS,
    WEB_SECRET_KEYS,
    logout_and_revoke_chatgpt_credentials,
)
from services.chatgpt_core.oauth_revoke import OAuthTokenRevocationResult
from services.chatgpt_core.plugin import ChatGPTPlatform
from services.chatgpt_core.web_logout import WebLogoutResult


def _oauth_result(token_type, *, success=True, status="revoked", http_status=200, verification=0):
    return OAuthTokenRevocationResult(
        token_type=token_type,
        success=success,
        status=status,
        http_status=http_status,
        error_code="" if success else "upstream_error",
        error_message="" if success else "upstream failed",
        verification_http_status=verification,
    )


def test_full_logout_clears_every_confirmed_component_and_never_exposes_secrets():
    with mock.patch(
        "services.chatgpt_core.credential_logout.logout_chatgpt_web_session",
        return_value=WebLogoutResult(True, status_code=200, used_session_cookie=True),
    ), mock.patch(
        "services.chatgpt_core.credential_logout.revoke_openai_oauth_token",
        side_effect=[
            _oauth_result("refresh_token"),
            _oauth_result("access_token", verification=401),
        ],
    ) as revoke:
        result = logout_and_revoke_chatgpt_credentials(
            cookies="cookie-secret",
            session_token="session-secret",
            access_token="at-secret",
            refresh_token="rt-secret",
            id_token="id-secret",
            client_id="client-demo",
            proxy_url="http://proxy.example:8080",
        )

    assert result.success is True
    assert result.status == "completed"
    assert result.clear_account_token is True
    assert result.auth_material_changed is True
    assert set(WEB_SECRET_KEYS + ACCESS_TOKEN_KEYS + REFRESH_TOKEN_KEYS + ID_TOKEN_KEYS).issubset(
        set(result.remove_extra_keys)
    )
    assert revoke.call_args_list[0].kwargs["token_type"] == "refresh_token"
    assert revoke.call_args_list[0].kwargs["client_id"] == "client-demo"
    assert revoke.call_args_list[1].kwargs["token_type"] == "access_token"
    public_result = str(result.audit_payload()) + str(result.logs)
    for secret in ("cookie-secret", "session-secret", "at-secret", "rt-secret", "id-secret"):
        assert secret not in public_result


def test_full_logout_partial_failure_only_removes_successful_material():
    with mock.patch(
        "services.chatgpt_core.credential_logout.logout_chatgpt_web_session",
        return_value=WebLogoutResult(True, status_code=200, used_session_cookie=True),
    ), mock.patch(
        "services.chatgpt_core.credential_logout.revoke_openai_oauth_token",
        side_effect=[
            _oauth_result("refresh_token", success=False, status="failed", http_status=500),
            _oauth_result("access_token", verification=401),
        ],
    ):
        result = logout_and_revoke_chatgpt_credentials(
            cookies="cookie-secret",
            access_token="at-secret",
            refresh_token="rt-secret",
            id_token="id-secret",
        )

    assert result.success is False
    assert result.status == "partial"
    assert result.clear_account_token is True
    assert set(WEB_SECRET_KEYS + ACCESS_TOKEN_KEYS).issubset(set(result.remove_extra_keys))
    assert not set(REFRESH_TOKEN_KEYS).intersection(result.remove_extra_keys)
    assert not set(ID_TOKEN_KEYS).intersection(result.remove_extra_keys)
    assert result.components["refresh_token"]["status"] == "failed"


def test_divergent_access_token_copies_are_all_verified_before_any_copy_is_removed():
    with mock.patch(
        "services.chatgpt_core.credential_logout.revoke_openai_oauth_token",
        side_effect=[
            _oauth_result("access_token", verification=401),
            _oauth_result("access_token", success=False, status="failed", http_status=200),
        ],
    ):
        result = logout_and_revoke_chatgpt_credentials(
            access_token="at-primary",
            access_tokens=("at-extra", "at-primary"),
        )

    assert result.success is False
    assert result.components["access_token"]["count"] == 2
    assert result.components["access_token"]["completed_count"] == 1
    assert result.clear_account_token is False
    assert not set(ACCESS_TOKEN_KEYS).intersection(result.remove_extra_keys)


def test_oauth_session_creation_failure_is_partial_safe_and_redacted():
    with mock.patch(
        "services.chatgpt_core.credential_logout.create_openai_oauth_session",
        side_effect=RuntimeError("proxy failed while sending rt-sensitive"),
    ):
        result = logout_and_revoke_chatgpt_credentials(refresh_token="rt-sensitive")

    assert result.success is False
    assert result.status == "failed"
    assert result.components["refresh_token"]["error_code"] == "revoke_session_error"
    assert "rt-sensitive" not in str(result.audit_payload())
    assert not set(REFRESH_TOKEN_KEYS).intersection(result.remove_extra_keys)


def test_full_logout_with_no_material_is_success_without_fake_lifecycle_change():
    result = logout_and_revoke_chatgpt_credentials()

    assert result.success is True
    assert result.status == "completed"
    assert result.remove_extra_keys == ()
    assert result.clear_account_token is False
    assert result.auth_material_changed is False
    assert all(component["status"] == "absent" for component in result.components.values())


def test_platform_exposes_web_only_and_destructive_logout_as_separate_actions():
    platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
    actions = {action["id"]: action for action in platform.get_platform_actions()}

    assert actions["logout_web_session"]["label"] == "退出 ChatGPT 网页会话"
    destructive = actions["logout_and_revoke_tokens"]
    assert destructive["label"] == "彻底退出并撤销 AT/RT"
    assert destructive["params"][0]["key"] == "confirm_revoke_all"
    assert destructive["params"][0]["default"] is False

    account = Account(
        platform="chatgpt",
        email="demo@example.com",
        password="password",
        token="",
        status=AccountStatus.REGISTERED,
        extra={},
    )
    rejected = platform.execute_action("logout_and_revoke_tokens", account, {})
    assert rejected == {"ok": False, "error": "请确认彻底退出并撤销当前账号的 AT/RT"}


def test_web_only_logout_updates_web_lifecycle_without_clearing_oauth_tokens():
    platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
    account = Account(
        platform="chatgpt",
        email="demo@example.com",
        password="password",
        token="at-primary",
        status=AccountStatus.REGISTERED,
        extra={
            "access_token": "at-primary",
            "refresh_token": "rt-primary",
            "cookie": "cookie-header",
            "sessionToken": "session-token",
        },
    )

    with mock.patch(
        "services.chatgpt_core.plugin.resolve_default_chatgpt_proxy",
        return_value=None,
    ), mock.patch(
        "services.chatgpt_core.web_logout.logout_chatgpt_web_session",
        return_value=WebLogoutResult(True, status_code=200, used_session_cookie=True),
    ) as logout:
        result = platform.execute_action(
            "logout_web_session",
            account,
            {"confirm_logout": True},
        )

    assert result["ok"] is True
    assert result["account_auth_material_changed"] is True
    assert set(WEB_SECRET_KEYS).issubset(set(result["account_extra_remove"]))
    assert result.get("account_token_clear") is None
    assert not set(ACCESS_TOKEN_KEYS + REFRESH_TOKEN_KEYS).intersection(result["account_extra_remove"])
    assert logout.call_args.kwargs["cookies"] == "cookie-header"
    assert logout.call_args.kwargs["session_token"] == "session-token"


class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, value):
        self.added.append(value)


def test_action_persistence_clears_primary_token_and_updates_auth_lifecycle():
    account = AccountModel(
        platform="chatgpt",
        email="demo@example.com",
        password="password",
        token="at-primary",
    )
    extra = {
        "access_token": "at-extra",
        "refresh_token": "rt-extra",
        "id_token": "id-extra",
        "session_token": "session-extra",
        "cookies": "cookie-extra",
        "web_session_expires_at": "2026-08-24T00:00:00Z",
    }
    extra[LIFECYCLE_EXTRA_KEY] = build_account_lifecycle_projection(account, extra)
    account.set_extra(extra)
    result = {
        "ok": True,
        "data": {"message": "done"},
        "account_extra_remove": list(WEB_SECRET_KEYS + ACCESS_TOKEN_KEYS + REFRESH_TOKEN_KEYS + ID_TOKEN_KEYS),
        "account_extra_patch": {
            "chatgpt_credential_logout": {
                "schema_version": 1,
                "status": "completed",
            }
        },
        "account_token_clear": True,
        "account_auth_material_changed": True,
        "account_auth_material_operation": "logout_and_revoke_tokens",
    }
    session = _FakeSession()

    _apply_action_result("chatgpt", "logout_and_revoke_tokens", account, result, session)

    saved = account.get_extra()
    assert account.token == ""
    for key in WEB_SECRET_KEYS + ACCESS_TOKEN_KEYS + REFRESH_TOKEN_KEYS + ID_TOKEN_KEYS:
        assert key not in saved
    assert saved["chatgpt_credential_logout"]["status"] == "completed"
    lifecycle = saved[LIFECYCLE_EXTRA_KEY]
    assert lifecycle["material"] == {
        "type": "unknown",
        "has_access_token": False,
        "has_refresh_token": False,
        "has_session_token": False,
        "has_cookies": False,
    }
    assert lifecycle["web_session"] == {
        "expires_at": "",
        "expiry_source": "",
        "observed_at": "",
    }
    assert session.added
