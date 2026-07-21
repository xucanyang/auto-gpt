from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_PAGE = ROOT / "frontend" / "src" / "pages" / "Accounts.tsx"
ACCOUNTS_QUERY = ROOT / "frontend" / "src" / "features" / "accounts" / "hooks" / "useAccountsQuery.ts"
SETTINGS_PAGE = ROOT / "frontend" / "src" / "pages" / "Settings.tsx"


def test_account_list_sends_expiry_then_registration_sort_and_defaults_registration_asc():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    query = ACCOUNTS_QUERY.read_text(encoding="utf-8")

    assert "const ACCOUNT_CREATED_AT_SORT_FIELD = 'created_at'" in page
    assert "`${SUBSCRIPTION_EXPIRY_SORT_FIELD},${ACCOUNT_CREATED_AT_SORT_FIELD}`" in page
    assert "`${subscriptionExpirySortOrder},${registrationSortOrder}`" in page
    assert "const registrationTableSortOrder = registrationSortOrder === 'desc' ? 'descend' : 'ascend'" in page
    assert "sorter: { multiple: 2 }" in page
    assert "sorter: { multiple: 1 }" in page
    assert "params.set('sort_by', sortBy)" in query
    assert "params.set('sort_order', sortOrder)" in query


def test_account_page_has_one_local_status_action_and_global_sync_settings():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    settings = SETTINGS_PAGE.read_text(encoding="utf-8")

    assert "配置代理与延时" not in page
    assert "key: `probe:${getStatusSyncScope()}`" in page
    assert "chatgpt_local_status_probe_concurrency" in settings
    assert "chatgpt_local_status_probe_unique_exit_ip_enabled" in settings
    assert "chatgpt_local_status_probe_delay_seconds" in settings
    assert "chatgpt_local_status_probe_delay_max_seconds" in settings
