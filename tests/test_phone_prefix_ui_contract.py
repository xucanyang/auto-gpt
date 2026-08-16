from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_PAGE = ROOT / "frontend" / "src" / "pages" / "Accounts.tsx"
PHONE_POOL_PAGE = ROOT / "frontend" / "src" / "pages" / "PhonePool.tsx"
RESULTS_TABLE = ROOT / "frontend" / "src" / "components" / "phone-binding" / "PhoneBindingResultsTable.tsx"


def test_phone_binding_panel_uses_row_capacity_and_sample_language():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")

    assert "label: '部分可用'" in page
    assert "可分配 ${phoneBindingLimitedCapacity}" in page
    assert "phoneBindingLimitedAvailablePhones" in page
    assert "单号实际可分配" in page
    assert "phoneBindingLimitedCapacity" in page
    assert "{ label: '仅失败样本', value: 'rejected' }" in page
    assert "仅不可用号段" not in page
    assert "只复测 OpenAI 拒绝过的号段" not in page


def test_phone_binding_panel_supports_prefix_row_filters_and_full_unavailable_snapshot():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")

    assert "type PhonePoolMode = 'normal' | 'prefix_limited' | 'prefix_sample' | 'unavailable_numbers'" in page
    assert "{ label: '不可用号码全量复测', value: 'unavailable_numbers' }" in page
    assert "{ label: '可用号码', value: 'available' }" in page
    assert "{ label: '不可用号码', value: 'unavailable' }" in page
    assert "{ label: '全部号码', value: 'all' }" in page
    assert "phone_number_filter: phoneNumberFilter" in page
    assert "prefix_number_filter: phoneNumberFilter" in page
    assert "unavailable_number_test_enabled: unavailableNumberTestEnabled" in page
    assert "phoneBindingLimitedUnavailablePhones" in page
    assert "不可用 ${cannotSend}" in page
    assert "label: '无可用号码'" not in page
    assert "候选号码 {phoneBindingCandidatePhoneCount}" in page
    assert "未覆盖号码 {phoneBindingUncoveredPhoneCount}" in page
    assert "disabled={phoneBindingPoolMode !== 'normal'}" in page


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
