from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_PAGE = ROOT / "frontend" / "src" / "pages" / "Accounts.tsx"
REGISTER_MODAL = (
    ROOT / "frontend" / "src" / "features" / "auth" / "components" / "RegisterTaskModal.tsx"
)
REGISTER_PAGE = ROOT / "frontend" / "src" / "pages" / "RegisterTaskPage.tsx"


def _function_block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_registration_modal_uses_server_profile_instead_of_stale_browser_overrides():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    modal = REGISTER_MODAL.read_text(encoding="utf-8")

    assert "auto-chatgpt.register-form-settings.v2." in page
    assert "taskProxySettingsFromConfig(cfg, savedSettings)" not in page
    assert "const proxySettings = taskProxySettingsFromConfig(cfg)" in page
    assert "loadConfigCache({ force: true })" in page
    assert "const configCacheRef = useRef<Record<string, any> | null>(null)" in page
    assert "if (!options.force && configCacheRef.current) return configCacheRef.current" in page
    assert "configCacheRef.current = cfg" in page
    assert "const hasSavedMailProfile = parseBooleanConfigValue(savedSettings.register_mail_profile_saved)" in page
    assert "normalizeRegisterMailProviderOverride(savedSettings.mail_provider_override)" in page
    assert "mail_provider_override: savedProviderOverride" in page
    assert "tempmail_fixed_domains: tempmailFixedDomains" in page
    assert "savedSettings.chatgpt_register_otp_wait_seconds" not in page
    assert 'label="邮箱服务（本任务默认）"' in modal
    assert '不会改写全局配置' in modal


def test_start_registration_does_not_write_task_values_back_to_global_config():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    standalone_page = REGISTER_PAGE.read_text(encoding="utf-8")
    start_block = _function_block(
        page,
        "const handleRegister = async () => {",
        "const handleDetailSave = async () => {",
    )
    save_block = _function_block(
        page,
        "const handleSaveRegisterSettings = async () => {",
        "const handleRegister = async () => {",
    )

    assert "saveTaskProxySettingsToConfig(values)" not in start_block
    assert "method: 'PUT'" not in start_block
    assert "saveTaskProxySettingsToConfig(settingsPayload)" in save_block
    assert "chatgpt_register_otp_wait_seconds: String(" in save_block
    assert "chatgpt_register_otp_resend_wait_seconds: String(" in save_block
    assert "chatgpt_register_otp_account_budget_seconds: String(" in save_block
    assert "mail_provider_override: settingsPayload.mail_provider_override" in save_block
    assert "tempmail_fixed_domains: settingsPayload.tempmail_fixed_domains" in save_block
    assert "register_mail_profile_saved: true" in save_block
    assert "saveTaskProxySettingsToConfig" not in standalone_page
    assert "method: 'PUT'" not in standalone_page
