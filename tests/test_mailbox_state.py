import json

from services.chatgpt_core.mailbox_state import (
    bound_before_ids,
    export_mailbox_state_config,
    sanitize_mailbox_state,
)


def test_hme_state_sanitizer_keeps_recovery_identity_and_drops_global_state():
    state = {
        "provider": "hme_ready_api",
        "email": "alias@icloud.com",
        "account": {
            "email": "alias@icloud.com",
            "account_id": "lease-123",
            "extra": {
                "provider": "hme_ready_api",
                "mode": "helper_ready_api",
                "lease_id": "lease-123",
                "checkout_id": "checkout-123",
                "registration_id": "reg-123",
                "logical_address_id": "logical-123",
                "physical_alias_id": "physical-123",
                "platform": "ChatGPT",
                "lease_state": "checked_out",
                "physical_hme": "base@icloud.com",
                "logical_type": "tag",
                "tag": "f8k2mq",
                "tag_namespace": "random_tag",
                "tag_slot": 1,
                "hme": "alias@icloud.com",
                "forward_to": "forward@example.com",
                "forward_mailbox_id": "mailbox-123",
                "global_copy": "x" * 200_000,
            },
        },
        "before_ids": [f"message-{index:04d}" for index in range(600)],
        "config": {
            "icloud_hme_mode": "helper_ready_api",
            "icloud_cookie": "must-never-be-persisted-for-helper",
            "icloud_hme_helper_api_url": "http://helper.internal",
            "icloud_hme_helper_internal_key": "helper-secret",
            "tempmail_api_url": "http://tempmail.internal",
            "tempmail_api_key": "tempmail-secret",
            "chatgpt_gopay_batch_tasks": "g" * 750_000,
            "chatgpt_gopay_phone_pool": ["+10000000000"] * 1000,
            "chatgpt_account_filter_presets": {"items": ["x"] * 1000},
        },
        "proxy": "http://proxy.example:8080",
    }

    cleaned = sanitize_mailbox_state(state)

    assert cleaned["schema_version"] == 2
    assert cleaned["account"]["account_id"] == "lease-123"
    assert cleaned["account"]["extra"]["lease_id"] == "lease-123"
    assert cleaned["account"]["extra"]["registration_id"] == "reg-123"
    assert cleaned["account"]["extra"]["logical_address_id"] == "logical-123"
    assert cleaned["account"]["extra"]["physical_alias_id"] == "physical-123"
    assert cleaned["account"]["extra"]["platform"] == "chatgpt"
    assert cleaned["account"]["extra"]["tag"] == "f8k2mq"
    assert cleaned["account"]["extra"]["forward_to"] == "forward@example.com"
    assert "global_copy" not in cleaned["account"]["extra"]
    assert cleaned["config"]["icloud_hme_helper_api_url"] == "http://helper.internal"
    assert cleaned["config"]["tempmail_api_url"] == "http://tempmail.internal"
    assert "icloud_cookie" not in cleaned["config"]
    assert "chatgpt_gopay_batch_tasks" not in cleaned["config"]
    assert "chatgpt_gopay_phone_pool" not in cleaned["config"]
    assert "chatgpt_account_filter_presets" not in cleaned["config"]
    assert len(cleaned["before_ids"]) == 128
    assert len(json.dumps(cleaned, ensure_ascii=False).encode("utf-8")) < 32 * 1024


def test_provider_config_export_is_allowlist_not_prefix_or_exclusion_filter():
    source = {
        "tempmail_api_url": "http://tempmail.internal",
        "tempmail_api_key": "secret",
        "tempmail_mode": "fixed_domain",
        "tempmail_wait_timeout_seconds": 300,
        "tempmail_unrelated_batch_state": "must-not-pass",
        "chatgpt_gopay_batch_tasks": "must-not-pass",
        "_task_control": object(),
    }

    exported = export_mailbox_state_config("tempmail_local", source)

    assert exported == {
        "tempmail_api_url": "http://tempmail.internal",
        "tempmail_api_key": "secret",
        "tempmail_mode": "fixed_domain",
        "tempmail_wait_timeout_seconds": 300,
    }


def test_helper_marker_on_legacy_icloud_provider_also_drops_cookie_and_keeps_identity():
    cleaned = sanitize_mailbox_state({
        "provider": "icloud_hme",
        "email": "alias@icloud.com",
        "account": {
            "email": "alias@icloud.com",
            "account_id": "",
            "extra": {
                "provider": "icloud_hme",
                "mode": "helper_ready_api",
                "lease_id": "lease-1",
                "alias_key": "alias-key-1",
                "helper_account_id": "helper-account-1",
                "refresh_token": "must-not-cross-provider-boundary",
            },
        },
        "config": {
            "icloud_cookie": "must-not-survive",
            "icloud_hme_helper_api_url": "http://helper.internal",
            "icloud_hme_helper_internal_key": "helper-key",
        },
    })

    assert cleaned["account"]["account_id"] == "lease-1"
    assert cleaned["account"]["extra"]["alias_key"] == "alias-key-1"
    assert cleaned["account"]["extra"]["helper_account_id"] == "helper-account-1"
    assert "refresh_token" not in cleaned["account"]["extra"]
    assert cleaned["config"]["icloud_hme_mode"] == "helper_ready_api"
    assert "icloud_cookie" not in cleaned["config"]


def test_before_ids_are_deterministic_deduplicated_and_byte_bounded():
    values = [f"id-{index:04d}-{'x' * 80}" for index in range(500)]
    values.extend(values[:20])

    bounded = bound_before_ids(values, max_items=256, max_bytes=4096)

    assert bounded == sorted(set(bounded))
    assert len(bounded) <= 256
    assert len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) <= 4096


def test_invalid_or_providerless_state_is_not_persisted():
    assert sanitize_mailbox_state(None) == {}
    assert sanitize_mailbox_state({}) == {}
    assert sanitize_mailbox_state({"config": {"tempmail_api_key": "secret"}}) == {}
