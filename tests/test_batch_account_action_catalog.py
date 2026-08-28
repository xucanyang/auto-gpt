from core.base_platform import RegisterConfig
from services.chatgpt_core import ChatGPTPlatform


def test_platform_exposes_only_safe_generic_batch_actions_to_the_account_toolbar():
    platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
    actions = {action["id"]: action for action in platform.get_platform_actions()}
    generic_batch_ids = {
        action_id
        for action_id, action in actions.items()
        if action.get("batch", {}).get("mode") == "generic"
    }

    assert generic_batch_ids == {
        "refresh_token",
        "refresh_web_session",
        "logout_web_session",
        "logout_and_revoke_tokens",
        "upload_cpa",
        "upload_codex_proxy",
    }
    assert actions["refresh_token"]["batch"]["group"] == "authentication"
    assert actions["upload_cpa"]["batch"]["group"] == "integration"
    assert actions["change_email"].get("batch") is None


def test_destructive_batch_actions_require_explicit_selected_scope_confirmation():
    platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
    actions = {action["id"]: action for action in platform.get_platform_actions()}

    web_logout = actions["logout_web_session"]["batch"]
    assert web_logout["selected_only"] is True
    assert web_logout["danger"] == "warning"
    assert web_logout["confirmation_param"] == "confirm_logout"
    assert "所选账号" in web_logout["confirmation_label"]

    revoke_all = actions["logout_and_revoke_tokens"]["batch"]
    assert revoke_all["selected_only"] is True
    assert revoke_all["danger"] == "danger"
    assert revoke_all["confirmation_param"] == "confirm_revoke_all"
    assert "所选账号" in revoke_all["confirmation_label"]
