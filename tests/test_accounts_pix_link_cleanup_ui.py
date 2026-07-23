from pathlib import Path
import re


ACCOUNTS_PAGE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages" / "Accounts.tsx"
ACCOUNTS_TOOLBAR = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "features"
    / "accounts"
    / "components"
    / "AccountsToolbar.tsx"
)
REGISTER_TASK_MODAL = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "features"
    / "auth"
    / "components"
    / "RegisterTaskModal.tsx"
)
ACCOUNT_DETAIL_MODAL = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "features"
    / "accounts"
    / "components"
    / "AccountDetailModal.tsx"
)
PIX_LINK_SCAN_MODAL = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "features"
    / "accounts"
    / "components"
    / "PixLinkScanModal.tsx"
)


def test_payment_link_scan_covers_all_types_defaults_collapsed_and_allows_five_state_deletion():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    toolbar = ACCOUNTS_TOOLBAR.read_text(encoding="utf-8")
    task_modal = REGISTER_TASK_MODAL.read_text(encoding="utf-8")
    detail_modal = ACCOUNT_DETAIL_MODAL.read_text(encoding="utf-8")
    scan_modal = PIX_LINK_SCAN_MODAL.read_text(encoding="utf-8")

    assert "扫描支付链接" in toolbar
    assert "pix_scan" in toolbar
    assert "onScanPixLinks" in toolbar
    assert "setPaymentLinkMenuOpen(false)" in toolbar
    assert "setMoreOperationMenuOpen(false)" in toolbar
    assert "window.setTimeout(onScanPixLinks, 0)" in toolbar
    assert "清理过期 PIX 链接" not in toolbar
    assert "清理已支付 PIX 链接" not in toolbar
    assert "清理支付已取消 PIX 链接" not in toolbar
    assert 'title="支付链接扫描"' in scan_modal
    for payment_type in (
        "hosted",
        "paypal",
        "ideal",
        "upi",
        "pix",
        "twint",
        "kakao_pay",
        "gopay",
        "team",
        "other",
    ):
        assert f"type: '{payment_type}'" in scan_modal
    assert "defaultActiveKey={[]}" in scan_modal
    assert "defaultExpandedTypes" not in scan_modal
    assert "提取后 15 分钟到期" in scan_modal
    assert "提取后 24 小时到期" in scan_modal
    assert "ALL_PAYMENT_LINK_CLEANUP_MODES" in scan_modal
    for mode in ("valid", "paid", "expired", "cancelled", "unknown"):
        assert f"'{mode}'" in scan_modal
    assert "label: '有效'" in scan_modal
    assert "label: '已支付'" in scan_modal
    assert "label: '过期'" in scan_modal
    assert "label: '支付已取消'" in scan_modal
    assert "label: '状态未知'" in scan_modal
    assert "条链接状态暂时无法确认，默认保留" in scan_modal
    assert "可展开对应类型后人工删除" in scan_modal
    assert "PIX / UPI Stripe 实时查询" in scan_modal
    assert "direct_scan_success_links" in scan_modal
    assert "direct_scan_fallback_links" in scan_modal
    assert "已按本地证据归类" in scan_modal
    assert "cleanupMode: null" not in scan_modal
    assert "if (!mode || row.count <= 0) return null" in scan_modal
    assert "<DeleteOutlined />" in scan_modal
    assert "删除" in scan_modal
    assert "/tasks/chatgpt/payment-links/scan" in page
    assert "/tasks/chatgpt/payment-links/cleanup/preview" in page
    assert "/tasks/chatgpt/payment-links/cleanup/task" in page
    assert "cleanup_mode: cleanupMode" in page
    assert "eligible_links" in page
    assert "const { message: appMessage, modal: appModal } = App.useApp()" in page
    assert "appModal.confirm({" in page
    assert re.search(r"(?<![A-Za-z0-9_.])Modal\.confirm\(", page) is None
    assert "当前没有可删除的${paymentLabel}${cleanupMeta.title}" in page
    assert "以支付链接提取时间后 15 分钟为过期点" in page
    assert "以支付链接提取时间后 24 小时为过期点" in page
    assert "有效链接可能仍可使用，仅在确认不再需要时删除" in page
    assert "状态无法确认，系统默认保留；本次将按人工选择删除" in page
    assert "不会删除账号、支付生成历史、支付 CDK 或提交结果" in page
    assert "payment_link_deleted" in page

    assert "pix_payment_link_cleanup" in page
    assert "pix_cleanup" in page
    assert "setTaskModalMode('pix_cleanup')" in page
    assert "setTaskId(" in page
    assert "setTaskSnapshot(" in page
    assert "setRegisterModalOpen(true)" in page
    assert "'pix_cleanup'" in task_modal
    assert "ideal: 'iDEAL'" in task_modal
    assert "team: 'ChatGPT Team'" in task_modal
    assert "`${cleanupLabel} ${paymentLabel} 链接删除`" in task_modal
    assert "<TaskLogPanel" in task_modal
    assert "showTaskControls={taskModalMode !== 'pix_cleanup'}" in task_modal
    assert "onTaskDone" in task_modal
    assert "label: '已支付'" in detail_modal
    assert "label: '支付已取消'" in detail_modal
    assert "status === 'payment_link_deleted'" in detail_modal
