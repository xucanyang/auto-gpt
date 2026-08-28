from core.base_platform import RegisterConfig
from services.chatgpt_core import ChatGPTPlatform


TASK_ACTION_HANDLERS = {
    "probe_local_status": "probe_local_status",
    "sync_cliproxyapi_status": "account_action",
    "sync_sub2api_status": "account_action",
    "sync_oaipay_status": "account_action",
    "refresh_token": "account_action",
    "refresh_web_session": "account_action",
    "logout_web_session": "account_action",
    "logout_and_revoke_tokens": "account_action",
    "upload_cpa": "account_action",
    "upload_sub2api": "sub2api_upload",
    "upload_codex_proxy": "account_action",
    "upload_oaipay": "oaipay_upload",
}


def _actions_by_id():
    platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
    return {action["id"]: action for action in platform.get_platform_actions()}


def test_platform_exposes_one_task_execution_catalog_for_single_selected_and_filtered_scopes():
    actions = _actions_by_id()
    task_actions = {
        action_id: action
        for action_id, action in actions.items()
        if action.get("execution", {}).get("mode") == "task"
    }

    assert set(task_actions) == set(TASK_ACTION_HANDLERS)
    for action_id, expected_handler in TASK_ACTION_HANDLERS.items():
        execution = task_actions[action_id]["execution"]
        assert execution["handler"] == expected_handler
        assert execution["scopes"] == ["single", "selected", "filtered"]


def test_only_shared_parameter_actions_use_the_generic_account_action_runner():
    actions = _actions_by_id()
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
    assert actions["change_email"].get("execution") is None


def test_destructive_task_actions_allow_filtered_scope_but_still_require_confirmation():
    actions = _actions_by_id()

    web_logout = actions["logout_web_session"]["batch"]
    assert "selected_only" not in web_logout
    assert web_logout["scopes"] == ["single", "selected", "filtered"]
    assert web_logout["danger"] == "warning"
    assert web_logout["confirmation_param"] == "confirm_logout"
    assert "目标账号" in web_logout["confirmation_label"]

    revoke_all = actions["logout_and_revoke_tokens"]["batch"]
    assert "selected_only" not in revoke_all
    assert revoke_all["scopes"] == ["single", "selected", "filtered"]
    assert revoke_all["danger"] == "danger"
    assert revoke_all["confirmation_param"] == "confirm_revoke_all"
    assert "目标账号" in revoke_all["confirmation_label"]
