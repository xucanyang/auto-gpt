from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_PAGE = ROOT / "frontend" / "src" / "pages" / "Accounts.tsx"
FILTER_PRESET_BAR = ROOT / "frontend" / "src" / "features" / "accounts" / "components" / "FilterPresetBar.tsx"
ACCOUNTS_QUERY = ROOT / "frontend" / "src" / "features" / "accounts" / "hooks" / "useAccountsQuery.ts"


def test_filter_preset_ui_reuses_one_entry_for_dynamic_and_fixed_content():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    bar = FILTER_PRESET_BAR.read_text(encoding="utf-8")

    assert "mode: 'dynamic' | 'fixed'" in page
    assert "account_ids: number[]" in page
    assert "label=\"组合内容\"" in page
    assert "{ value: 'dynamic', label: '筛选条件' }" in page
    assert "{ value: 'fixed', label: `固定账号 (${filterPresetEditorAccountIds.length})` }" in page
    assert "selectedIds.length > 0 ? 'fixed' : 'dynamic'" in page
    assert "保存已选账号" in page
    assert "固定账号成员" in page

    assert "<Text strong style={{ fontSize: 13 }}>筛选组合</Text>" in bar
    assert "保存已选账号" in bar
    assert "preset.mode === 'fixed'" in bar
    assert "固定成员：已保存" in bar


def test_fixed_filter_preset_uses_short_preset_id_query_and_existing_batch_scope():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    query = ACCOUNTS_QUERY.read_text(encoding="utf-8")

    assert "filterPresetId: activeFixedFilterPresetId" in page
    assert "pendingFixedPresetResolutionRef" in page
    assert "fixedScope.resolved_account_ids" in page
    assert "body.account_ids = accountIds" in page
    assert "activeFilterPreset?.mode === 'fixed'" in page

    assert "filterPresetId?: string" in query
    assert "params.set('filter_preset_id', filterPresetId)" in query
    assert "fixed_preset?:" in query
    assert "resolved_account_ids: number[]" in query
