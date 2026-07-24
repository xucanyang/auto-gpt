from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = (ROOT / "frontend" / "src" / "pages" / "Accounts.tsx").read_text(encoding="utf-8")
PROXIES = (ROOT / "frontend" / "src" / "pages" / "Proxies.tsx").read_text(encoding="utf-8")
TASK_PROXY = (ROOT / "frontend" / "src" / "lib" / "taskProxySettings.ts").read_text(encoding="utf-8")


def test_dynamic_proxy_probe_switch_is_part_of_the_saved_global_payload():
    assert "dynamic_proxy_probe_enabled: dynamicProxyProbe" in PROXIES
    assert "Object.prototype.hasOwnProperty.call(rawValues, 'dynamic_proxy_probe_enabled')" in TASK_PROXY
    assert "data.dynamic_proxy_probe_enabled = booleanWithDefault" in TASK_PROXY


def test_phone_binding_programmatic_changes_use_the_same_local_persistence_path():
    assert "const updatePhoneBindingSettings = (patch: Record<string, unknown>) =>" in ACCOUNTS
    assert "updatePhoneBindingSettings({\n                          phone_pool_mode: mode" in ACCOUNTS
    assert "updatePhoneBindingSettings({\n                      prefix_sms_probe_only: checked" in ACCOUNTS
    assert "updatePhoneBindingSettings({\n                      reuse_phone_until_unusable: checked" in ACCOUNTS


def test_other_account_panel_local_settings_parse_string_booleans_safely():
    assert "function boolWithDefault(value: unknown, fallback: boolean)" in ACCOUNTS
    assert "use_pool: boolWithDefault(raw.use_pool, DEFAULT_PHONE_BINDING_SETTINGS.use_pool)" in ACCOUNTS
    assert "use_pool: boolWithDefault(raw.use_pool, DEFAULT_BAXIGPT_CDK_SETTINGS.use_pool)" in ACCOUNTS
    assert "sms_api_test_mode: boolWithDefault(raw.sms_api_test_mode" in ACCOUNTS
