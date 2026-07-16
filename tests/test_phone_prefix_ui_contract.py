from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_PAGE = ROOT / "frontend" / "src" / "pages" / "Accounts.tsx"
PHONE_POOL_PAGE = ROOT / "frontend" / "src" / "pages" / "PhonePool.tsx"
RESULTS_TABLE = ROOT / "frontend" / "src" / "components" / "phone-binding" / "PhoneBindingResultsTable.tsx"


def test_phone_binding_panel_uses_row_capacity_and_sample_language():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")

    assert "label: '部分可用'" in page
    assert "实际可用" in page
    assert "phoneBindingLimitedAvailablePhones" in page
    assert "单号实际可分配" in page
    assert "phoneBindingLimitedCapacity" in page
    assert "{ label: '仅失败样本', value: 'rejected' }" in page
    assert "仅不可用号段" not in page
    assert "只复测 OpenAI 拒绝过的号段" not in page


def test_phone_pool_shows_mixed_prefixes_without_prefix_skip_filter():
    page = PHONE_POOL_PAGE.read_text(encoding="utf-8")

    assert "partial?: PhonePoolPrefixItem[]" in page
    assert "renderPrefixBlock('部分可用号段'" in page
    assert "'号码自身可用'" in page
    assert "{ value: 'prefix_blocked', label: '号段跳过' }" not in page
    assert "普通绑定已跳过这些号段" not in page


def test_prefix_sample_results_are_labeled_as_samples_not_prefix_eligibility():
    component = RESULTS_TABLE.read_text(encoding="utf-8")

    assert "成功样本" in component
    assert "失败样本" in component
    assert "样本混合" in component
    assert "negative_sample_prefixes" in component
    assert "复制可用号段" not in component
    assert "复制不可用号段" not in component
