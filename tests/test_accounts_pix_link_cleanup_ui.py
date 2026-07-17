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


def test_pix_link_scan_lists_exclusive_buckets_and_cleanup_is_confirmed():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    toolbar = ACCOUNTS_TOOLBAR.read_text(encoding="utf-8")
    task_modal = REGISTER_TASK_MODAL.read_text(encoding="utf-8")
    detail_modal = ACCOUNT_DETAIL_MODAL.read_text(encoding="utf-8")
    scan_modal = PIX_LINK_SCAN_MODAL.read_text(encoding="utf-8")

    assert "扫描 PIX 链接" in toolbar
    assert "pix_scan" in toolbar
    assert "onScanPixLinks" in toolbar
    assert "清理过期 PIX 链接" not in toolbar
    assert "清理已支付 PIX 链接" not in toolbar
    assert "清理支付已取消 PIX 链接" not in toolbar
    assert "PIX 链接扫描" in scan_modal
    assert "总 PIX 链接" in scan_modal
    assert "label: '有效'" in scan_modal
    assert "label: '已支付'" in scan_modal
    assert "label: '过期'" in scan_modal
    assert "label: '支付已取消'" in scan_modal
    assert "cleanupMode: null" in scan_modal
    assert "if (!mode) return null" in scan_modal
    assert "<DeleteOutlined />" in scan_modal
    assert "/tasks/chatgpt/payment-links/pix-cleanup/scan" in page
    assert "/tasks/chatgpt/payment-links/pix-cleanup/preview" in page
    assert "/tasks/chatgpt/payment-links/pix-cleanup/task" in page
    assert "cleanup_mode: cleanupMode" in page
    assert "eligible_links" in page
    assert "const { message: appMessage, modal: appModal } = App.useApp()" in page
    assert "appModal.confirm({" in page
    assert re.search(r"(?<![A-Za-z0-9_.])Modal\.confirm\(", page) is None
    assert "只清理账号当前 PIX 链接" in page
    assert "不会删除账号、支付生成历史、PIX CDK 或提交结果" in page

    assert "pix_payment_link_cleanup" in page
    assert "pix_cleanup" in page
    assert "setTaskModalMode('pix_cleanup')" in page
    assert "setTaskId(" in page
    assert "setTaskSnapshot(" in page
    assert "setRegisterModalOpen(true)" in page
    assert "当前没有可清理的${cleanupMeta.title}" in page

    assert "'pix_cleanup'" in task_modal
    assert "`${cleanupLabel} PIX 链接清理`" in task_modal
    assert "<TaskLogPanel" in task_modal
    assert "showTaskControls={taskModalMode !== 'pix_cleanup'}" in task_modal
    assert "onTaskDone" in task_modal
    assert "label: '已支付'" in detail_modal
    assert "label: '支付已取消'" in detail_modal
