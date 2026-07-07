from services.chatgpt_core.oauth_client import OAuthClient


def test_hme_ready_missing_lease_is_fatal_mailbox_error():
    exc = RuntimeError(
        "HME Ready API 调用失败: POST /api/hme-ready/mailboxes/m5tbftxrk28215/wait-code "
        "status=404 error=checkout_id 或 alias_id 不存在"
    )

    assert OAuthClient._is_fatal_mailbox_config_error(exc) is True


def test_hme_ready_missing_restored_lease_is_fatal_mailbox_error():
    exc = RuntimeError("icloud_hme helper_ready_api 当前任务缺少 lease_id")

    assert OAuthClient._is_fatal_mailbox_config_error(exc) is True
