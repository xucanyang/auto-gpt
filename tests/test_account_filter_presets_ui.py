from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_PAGE = ROOT / "frontend" / "src" / "pages" / "Accounts.tsx"
FILTER_PRESET_BAR = ROOT / "frontend" / "src" / "features" / "accounts" / "components" / "FilterPresetBar.tsx"
ACCOUNTS_QUERY = ROOT / "frontend" / "src" / "features" / "accounts" / "hooks" / "useAccountsQuery.ts"


def test_filter_preset_ui_renders_primary_and_secondary_rows_with_names_only():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    bar = FILTER_PRESET_BAR.read_text(encoding="utf-8")

    assert ">条件筛选组合</Text>" in bar
    assert ">固定账号组合</Text>" in bar
    assert "{ value: UNASSIGNED_SCOPE_VALUE, label: '未固定' }" in bar
    assert "label: preset.name" in bar
    assert "label: group.name" in bar
    assert "固定 ${" not in bar
    assert "account_count" not in bar

    assert "openCreateCurrentFilterPreset" in page
    assert "openCreateFixedGroup" in page
    assert "保存当前条件组合" in page
    assert "新建固定账号组合" in page
    assert "label=\"组合内容\"" not in page


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
