from core.task_proxy_config import (
    normalize_dynamic_proxy_snapshot,
    normalize_dynamic_proxy_update,
)


def test_dynamic_snapshot_promotes_legacy_fields_and_clears_them():
    result = normalize_dynamic_proxy_snapshot(
        {
            "task_proxy_mode": "dynamic",
            "task_proxy_url": "legacy-template",
            "task_proxy_country_code": "us",
        }
    )

    assert result.updates == {
        "dynamic_proxy_template": "legacy-template",
        "task_proxy_url": "",
        "dynamic_proxy_default_country": "US",
        "task_proxy_country_code": "",
    }
    assert result.template_source == "legacy_fallback"
    assert result.country_source == "legacy_fallback"
    assert result.report()["template"]["present"] is True
    assert "legacy-template" not in str(result.report())


def test_dynamic_snapshot_keeps_canonical_and_cleans_duplicate_legacy_fields():
    result = normalize_dynamic_proxy_snapshot(
        {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_template": "canonical-template",
            "task_proxy_url": "canonical-template",
            "dynamic_proxy_default_country": "JP",
            "task_proxy_country_code": "JP",
        }
    )

    assert result.updates == {
        "task_proxy_url": "",
        "task_proxy_country_code": "",
    }
    assert result.template_source == "canonical"
    assert result.country_source == "canonical"


def test_dynamic_snapshot_preserves_pre_upgrade_runtime_value_when_fields_conflict():
    result = normalize_dynamic_proxy_snapshot(
        {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_template": "canonical-template",
            "task_proxy_url": "legacy-runtime-template",
            "dynamic_proxy_default_country": "JP",
            "task_proxy_country_code": "US",
        }
    )

    assert result.updates == {
        "dynamic_proxy_template": "legacy-runtime-template",
        "task_proxy_url": "",
        "dynamic_proxy_default_country": "US",
        "task_proxy_country_code": "",
    }
    assert result.template_source == "legacy_runtime_conflict"
    assert result.country_source == "legacy_runtime_conflict"
    assert result.template_conflict is True
    assert result.country_conflict is True


def test_dynamic_snapshot_is_idempotent_after_updates_are_applied():
    original = {
        "task_proxy_mode": "dynamic",
        "task_proxy_url": "legacy-template",
        "task_proxy_country_code": "SG",
    }
    first = normalize_dynamic_proxy_snapshot(original)
    normalized = {**original, **first.updates}
    second = normalize_dynamic_proxy_snapshot(normalized)

    assert first.changed is True
    assert second.updates == {}
    assert second.changed is False


def test_non_dynamic_snapshot_keeps_legacy_fields_untouched():
    result = normalize_dynamic_proxy_snapshot(
        {
            "task_proxy_mode": "specified",
            "task_proxy_url": "specified-proxy",
            "task_proxy_country_code": "JP",
            "dynamic_proxy_template": "saved-dynamic-template",
        }
    )

    assert result.updates == {}
    assert result.mode == "specified"


def test_dynamic_update_promotes_old_client_legacy_payload_without_losing_current_template():
    result = normalize_dynamic_proxy_update(
        {
            "task_proxy_mode": "dynamic",
            "task_proxy_url": "",
            "task_proxy_country_code": "",
        },
        {
            "task_proxy_mode": "dynamic",
            "task_proxy_url": "legacy-template",
            "task_proxy_country_code": "JP",
        },
    )

    assert result["dynamic_proxy_template"] == "legacy-template"
    assert result["dynamic_proxy_default_country"] == "JP"
    assert result["task_proxy_url"] == ""
    assert result["task_proxy_country_code"] == ""


def test_dynamic_update_prefers_explicit_canonical_values_from_new_ui():
    result = normalize_dynamic_proxy_update(
        {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_template": "new-canonical-template",
            "dynamic_proxy_default_country": "us",
            "task_proxy_url": "stale-legacy-template",
            "task_proxy_country_code": "JP",
        },
        {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_template": "old-template",
            "dynamic_proxy_default_country": "SG",
        },
    )

    assert result["dynamic_proxy_template"] == "new-canonical-template"
    assert result["dynamic_proxy_default_country"] == "US"
    assert result["task_proxy_url"] == ""
    assert result["task_proxy_country_code"] == ""


def test_dynamic_partial_update_keeps_canonical_values_when_legacy_fields_conflict():
    result = normalize_dynamic_proxy_update(
        {
            "dynamic_proxy_default_country": "us",
            "dynamic_proxy_ip_retention_minutes": "120",
        },
        {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_template": "canonical-template",
            "task_proxy_url": "stale-legacy-template",
            "dynamic_proxy_default_country": "JP",
            "task_proxy_country_code": "DE",
        },
    )

    assert result["dynamic_proxy_template"] == "canonical-template"
    assert result["dynamic_proxy_default_country"] == "US"
    assert result["dynamic_proxy_ip_retention_minutes"] == "120"
    assert result["task_proxy_url"] == ""
    assert result["task_proxy_country_code"] == ""


def test_retention_only_update_is_a_true_field_patch():
    update = {"dynamic_proxy_ip_retention_minutes": "120"}
    current = {
        "task_proxy_mode": "dynamic",
        "dynamic_proxy_template": "canonical-template",
        "task_proxy_url": "stale-legacy-template",
        "dynamic_proxy_default_country": "JP",
        "task_proxy_country_code": "DE",
    }

    assert normalize_dynamic_proxy_update(update, current) == update


def test_specified_update_does_not_mutate_dynamic_canonical_values():
    update = {
        "task_proxy_mode": "specified",
        "task_proxy_url": "specified-proxy",
        "task_proxy_country_code": "US",
    }

    assert normalize_dynamic_proxy_update(update, {"dynamic_proxy_template": "dynamic-template"}) == update
