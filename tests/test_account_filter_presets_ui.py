from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_PAGE = ROOT / "frontend" / "src" / "pages" / "Accounts.tsx"
FILTER_PRESET_BAR = ROOT / "frontend" / "src" / "features" / "accounts" / "components" / "FilterPresetBar.tsx"
ACCOUNTS_QUERY = ROOT / "frontend" / "src" / "features" / "accounts" / "hooks" / "useAccountsQuery.ts"
SUBSCRIPTION_COUNTS = ROOT / "frontend" / "src" / "features" / "accounts" / "components" / "SubscriptionStatusCounts.tsx"
REGISTER_TASK_MODAL = ROOT / "frontend" / "src" / "features" / "auth" / "components" / "RegisterTaskModal.tsx"
REGISTRATION_PIPELINE = ROOT / "frontend" / "src" / "lib" / "registrationPipeline.ts"


def test_filter_preset_ui_renders_primary_and_secondary_rows_with_names_only():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    bar = FILTER_PRESET_BAR.read_text(encoding="utf-8")

    assert ">条件筛选组合</Text>" in bar
    assert ">固定账号组合</Text>" in bar
    assert "{ value: UNASSIGNED_SCOPE_VALUE, label: '未固定', searchText: '未固定' }" in bar
    assert "label: preset.name" in bar
    assert "label: fixedGroupLabel(group)" in bar
    assert "固定 ${" not in bar
    assert "account_count" not in bar

    assert "openCreateCurrentFilterPreset" in page
    assert "openCreateFixedGroup" in page
    assert "保存当前条件组合" in page
    assert "新建固定账号组合" in page
    assert "label=\"组合内容\"" not in page


def test_fixed_group_can_start_from_a_plain_account_selection_and_choose_parent():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    bar = FILTER_PRESET_BAR.read_text(encoding="utf-8")

    create_handler = page.split("const openCreateFixedGroup = useCallback", 1)[1].split(
        "const openCopyFilterPreset",
        1,
    )[0]
    create_gate = bar.split("const canCreateFixedGroup", 1)[1].split(
        "const createFixedGroupTooltip",
        1,
    )[0]

    assert "请先选择一级条件筛选组合" not in create_handler
    assert "DEFAULT_FIXED_GROUP_PARENT_PRESET_ID" in create_handler
    assert "activeFilterPreset" not in create_gate
    assert 'label="所属条件组合"' in page
    assert "请选择所属条件组合" in page
    assert "options={filterPresets.map((preset) => ({ value: preset.id, label: preset.name }))}" in page
    assert "secondaryFilterScope === 'unassigned' && selectedRowKeys.length > 0" in page


def test_fixed_group_conflict_has_an_explicit_move_and_retry_flow():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    save_handler = page.split("const saveFilterPresetForm = useCallback", 1)[1].split(
        "const overwritePresetWithCurrent",
        1,
    )[0]

    assert "move_conflicts: moveConflicts" in save_handler
    assert "FIXED_GROUP_MEMBER_CONFLICT" in save_handler
    assert "所选账号已有固定归属" in save_handler
    assert "移动并创建" in save_handler
    assert "saveFilterPresetForm({ moveConflicts: true })" in save_handler


def test_account_polling_honors_backend_freshness_and_pauses_for_selection():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    pipeline = REGISTRATION_PIPELINE.read_text(encoding="utf-8")

    polling_effect = page.split("const registrationPipelineActive = useMemo", 1)[1].split(
        "const handleAccountsPageSizeChange",
        1,
    )[0]
    assert "typeof pipeline.active === 'boolean'" in pipeline
    assert "return pipeline.active" in pipeline
    assert "selectedRowKeys.length > 0" in polling_effect
    assert "filterPresetEditorOpen" in polling_effect
    assert "accountsQuery.isLoading || accountsQuery.isPlaceholderData" in page
    assert "accountsQuery.isLoading || accountsQuery.isFetching" not in page


def test_fixed_group_and_local_refresh_subscription_counts_are_visible():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    bar = FILTER_PRESET_BAR.read_text(encoding="utf-8")
    counts = SUBSCRIPTION_COUNTS.read_text(encoding="utf-8")
    task_modal = REGISTER_TASK_MODAL.read_text(encoding="utf-8")

    assert 'title={<SubscriptionStatusCounts counts={group.subscription_counts} labels="short" splitUnknown />}' in bar
    assert 'overlayClassName="accounts-fixed-group-status-tooltip"' in bar
    assert 'color="#1f2937"' in bar
    assert 'labels="full"' in page
    assert "不可确认(u)" in counts and "待刷新(w)" in counts
    assert "label: 'u'" in counts and "label: 'w'" in counts
    assert "splitUnknown" in page
    assert "accounts-subscription-count-label" in counts
    assert "accounts-subscription-count-value" in counts
    assert "刷新后订阅分布" in task_modal
    assert "taskSnapshot?.meta?.subscription_counts" in task_modal
    assert "Promise.all([load(), loadFilterPresets(true)])" in page


def test_subscription_filter_splits_unconfirmable_and_pending_refresh_with_legacy_compatibility():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")

    assert "{ value: 'unconfirmable', text: '不可确认' }" in page
    assert "{ value: 'pending_refresh', text: '待刷新' }" in page
    assert "{ value: 'unknown', text: '未知 / 待刷新' }" not in page
    assert "function normalizeSubscriptionTypeFilterValues" in page
    assert "? ['unconfirmable', 'pending_refresh']" in page
    assert "normalize={normalizeSubscriptionTypeFilterValues}" in page


def test_fixed_group_scope_is_shared_by_list_and_filtered_tasks_without_auto_selection():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    query = ACCOUNTS_QUERY.read_text(encoding="utf-8")

    assert "primaryPresetId: activeFilterPresetId" in page
    assert "secondaryScope: activeFilterPresetId ? secondaryFilterScope : ''" in page
    assert "fixedGroupId: secondaryFilterScope === 'fixed' ? activeFixedGroupId : ''" in page
    assert "body.primary_preset_id = activeFilterPreset.id" in page
    assert "body.secondary_scope = secondaryFilterScope" in page
    assert "body.fixed_group_id = activeFixedGroup.id" in page
    assert "body.fixed_group_revision = activeFixedGroup.revision || 1" in page

    fixed_group_handler = page.split("const applyFixedGroup = useCallback", 1)[1].split("const clearFilterPreset", 1)[0]
    assert "setSelectedRowKeys([])" in fixed_group_handler
    assert "setSelectedRowKeys(group.account_ids)" not in fixed_group_handler

    assert "params.set('primary_preset_id', primaryPresetId)" in query
    assert "params.set('secondary_scope', secondaryScope)" in query
    assert "params.set('fixed_group_id', fixedGroupId)" in query
    assert "params.set('fixed_group_revision', String(fixedGroupRevision))" in query


def test_pinned_filter_preset_bar_renders_every_pinned_combination():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    bar = FILTER_PRESET_BAR.read_text(encoding="utf-8")

    assert "filterPresets.filter((item) => item.pinned)" in page
    assert "currentParentFixedGroups.filter((item) => item.pinned)" in page
    assert "slice(0, isMobile ? 4 : 8)" not in page
    assert "pinnedFilterPresets.map((preset) => renderShortcut" in bar
    assert "pinnedFixedGroups.map((group) => renderShortcut" in bar


def test_legacy_fixed_migration_requires_explicit_parent_selection():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")

    migration_handler = page.split("const openFixedMigration = useCallback", 1)[1].split(
        "const moveFixedMigrationPriority",
        1,
    )[0]
    assert "setFilterPresetManageOpen(false)" in migration_handler
    assert "setFixedMigrationParentById({})" in migration_handler
    assert "fallbackParentId" not in migration_handler
    assert "outside_parent_account_count" in page
