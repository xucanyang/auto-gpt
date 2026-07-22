from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_PAGE = ROOT / "frontend" / "src" / "pages" / "Accounts.tsx"
ACCOUNTS_QUERY = (
    ROOT
    / "frontend"
    / "src"
    / "features"
    / "accounts"
    / "hooks"
    / "useAccountsQuery.ts"
)


def test_payment_link_filter_separates_current_link_from_success_history():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    query = ACCOUNTS_QUERY.read_text(encoding="utf-8")

    assert "{ value: 'none', text: '当前无链接' }" in page
    assert "{ value: 'true', text: '已成功提取' }" in page
    assert "{ value: 'false', text: '从未成功提取' }" in page
    assert "primaryLabel: '当前链接类型'" in page
    assert "label: '提取记录'" in page
    assert "paymentLinkGenerated: normalizePaymentLinkGeneratedFilterValues(next)" in page
    assert "return normalized.includes('true') && normalized.includes('false') ? [] : normalized" in page

    assert "payment_link_generated: string" in page
    assert "'payment_link_generated'," in page
    assert "payment_link_generated: columnFilters.paymentLinkGenerated.join(',')" in page
    assert "paymentLinkGenerated: columnFilters.paymentLinkGenerated.join(',')" in page
    assert "paymentLinkGenerated: normalized.columnFilters.paymentLinkGenerated" in page
    assert "paymentLinkGenerated: values.paymentLinkGenerated" in page

    assert "paymentLinkGenerated?: string" in query
    assert "paymentLinkGenerated," in query
    assert "params.set('payment_link_generated', paymentLinkGenerated)" in query


def test_payment_link_generation_dialog_explains_variant_aware_history_guard():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")

    assert "默认只跳过相同" in page
    assert "Team 参数变体" in page
    assert "Plus 配置" in page
    assert "已支付、已订阅、已失效或正在生成的账号仍会跳过" in page


def test_payment_link_cell_keeps_cleaned_tombstones_visible_without_link_actions():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")

    assert "expired_cleaned: { color: 'warning', label: '已过期清理' }" in page
    assert "paid_cleaned: { color: 'success', label: '已支付清理' }" in page
    assert "cancelled_cleaned: { color: 'warning', label: '支付已取消清理' }" in page
    assert "link.cleaned_at || link.link_status_updated_at" in page
    assert "const displayTimeLabel = cleanedStatusMeta ? '清理时间' : '生成时间'" in page
    assert "generated ? '已成功提取' : '尚未提取'" in page
    assert "if (!url && !status && !generated)" in page
    assert "{url ? (" in page
    assert "const [copiedPaymentLinkUrlsByAccountId, setCopiedPaymentLinkUrlsByAccountId]" in page
    assert "const ok = await copyText(normalizedUrl)" in page
    assert "if (!ok) return" in page
    assert "copiedPaymentLinkUrlsByAccountId.get(accountId) === url" in page
    assert "title={paymentLinkCopied ? '已复制支付链接' : '复制支付链接'}" in page
    assert "aria-label={paymentLinkCopied ? '已复制支付链接' : '复制支付链接'}" in page
    assert "icon={paymentLinkCopied ? <CheckOutlined /> : <CopyOutlined />}" in page
    assert "background: token.colorWarningBg" in page
    assert "void copyPaymentLink(record, url)" in page
    assert 'title="打开支付链接"' in page
