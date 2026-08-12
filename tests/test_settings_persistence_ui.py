from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = (ROOT / "frontend" / "src" / "pages" / "Accounts.tsx").read_text(encoding="utf-8")
PROXIES = (ROOT / "frontend" / "src" / "pages" / "Proxies.tsx").read_text(encoding="utf-8")
SETTINGS = (ROOT / "frontend" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")
EMAIL_RECHECK = (ROOT / "frontend" / "src" / "pages" / "CustomEmailRecheckPage.tsx").read_text(encoding="utf-8")
TASK_PROXY = (ROOT / "frontend" / "src" / "lib" / "taskProxySettings.ts").read_text(encoding="utf-8")


def test_dynamic_proxy_probe_switch_is_part_of_the_saved_global_payload():
    assert "dynamic_proxy_probe_enabled: dynamicProxyProbe" in PROXIES
    assert "hasOwn(rawValues, 'dynamic_proxy_probe_enabled')" in TASK_PROXY
    assert "putBoolean(data, 'dynamic_proxy_probe_enabled'" in TASK_PROXY


def test_task_proxy_global_patch_omits_unprovided_defaults():
    assert "export function buildTaskProxyConfigPatch(values: unknown)" in TASK_PROXY
    assert "hasOwn(rawValues, 'dynamic_proxy_ip_retention_minutes')" in TASK_PROXY
    assert "data.dynamic_proxy_ip_retention_minutes = String(settings.dynamic_proxy_ip_retention_minutes)" not in TASK_PROXY
    assert "if (Object.keys(data).length === 0) return settings" in TASK_PROXY
    assert "payload.dynamic_proxy_ip_retention_minutes = settings.dynamic_proxy_ip_retention_minutes" in TASK_PROXY
    assert "isProvided(rawValues.dynamic_proxy_ip_retention_minutes)" in TASK_PROXY


def test_task_submits_do_not_write_proxy_values_back_to_shared_config():
    phone_submit = ACCOUNTS[ACCOUNTS.index("const submitPhoneBindingTest = async () => {"):ACCOUNTS.index("const openBaxiCdkSubmit", ACCOUNTS.index("const submitPhoneBindingTest = async () => {"))]
    email_submit = EMAIL_RECHECK[EMAIL_RECHECK.index("const handleSubmit = async () => {"):EMAIL_RECHECK.index("const handleSub2ApiFileSelect")]
    bulk_submit = EMAIL_RECHECK[EMAIL_RECHECK.index("const handleBulkSubmit = async () => {"):EMAIL_RECHECK.index("  return (", EMAIL_RECHECK.index("const handleBulkSubmit = async () => {"))]
    assert "saveTaskProxySettingsToConfig" not in phone_submit
    assert "saveTaskProxySettingsToConfig" not in email_submit
    assert "saveTaskProxySettingsToConfig" not in bulk_submit


def test_settings_snapshot_only_sends_changed_proxy_fields():
    assert "const initialTaskProxyValuesRef = useRef<Record<string, unknown> | null>(null)" in SETTINGS
    assert "const changedTaskProxyKeys = new Set(" in SETTINGS
    assert "if (!changedTaskProxyKeys.has(key)) delete payload[key]" in SETTINGS
    assert "data: payload" in SETTINGS


def test_dynamic_node_copy_is_explicit_about_task_override_scope():
    assert "动态节点（本次任务可覆盖）" in ACCOUNTS
    assert "仅覆盖本次测活任务" in EMAIL_RECHECK
    assert "保存全局渠道" in PROXIES


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
