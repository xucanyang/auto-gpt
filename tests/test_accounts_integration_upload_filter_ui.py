from pathlib import Path


ACCOUNTS_PAGE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages" / "Accounts.tsx"


def test_sub2api_and_oaipay_filters_use_one_binary_upload_contract():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")

    options_block = page.split("const INTEGRATION_UPLOAD_FILTER_OPTIONS = [", 1)[1].split("]", 1)[0]
    assert "{ value: 'uploaded', text: '已上传' }" in options_block
    assert "{ value: 'not_uploaded', text: '未上传' }" in options_block
    for retired_option in (
        "cross_workspace_only",
        "deleted_exact_match",
        "ambiguous",
        "unreachable",
    ):
        assert retired_option not in options_block

    assert "const SUB2API_FILTER_OPTIONS = INTEGRATION_UPLOAD_FILTER_OPTIONS" in page
    assert "const OAIPAY_FILTER_OPTIONS = INTEGRATION_UPLOAD_FILTER_OPTIONS" in page
    assert "function normalizeIntegrationUploadFilterValues" in page
    assert "remoteState === 'uploaded'" in page
    assert "remoteState === 'exists'" in page
    assert "String(lastUpload.status || '').trim().toLowerCase() === 'success'" in page
    assert "? { color: 'success', label: '已上传' }" in page
    assert ": { color: 'default', label: '未上传' }" in page
    assert "account.sub2apiSync && typeof account.sub2apiSync === 'object'" in page
    assert "account.oaipaySync && typeof account.oaipaySync === 'object'" in page


def test_filter_preset_editor_converts_all_text_options_to_select_labels():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")

    for option_name in (
        "STATUS_FILTER_OPTIONS",
        "AUTH_TYPE_FILTER_OPTIONS",
        "PHONE_BINDING_STATE_FILTER_OPTIONS",
        "PAYMENT_LINK_PLATFORM_FILTER_OPTIONS",
        "SUBSCRIPTION_TYPE_FILTER_OPTIONS",
        "ACCOUNT_VALIDITY_FILTER_OPTIONS",
        "SUB2API_FILTER_OPTIONS",
        "OAIPAY_FILTER_OPTIONS",
        "SUBMISSION_STATE_FILTER_OPTIONS",
        "HAS_SUBMITTED_FILTER_OPTIONS",
    ):
        assert f"options={{toSelectOptions({option_name})}}" in page
        assert f"options={{{option_name}}}" not in page
