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


def test_expired_pix_link_cleanup_is_previewed_confirmed_and_refetched():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    toolbar = ACCOUNTS_TOOLBAR.read_text(encoding="utf-8")
    task_modal = REGISTER_TASK_MODAL.read_text(encoding="utf-8")

    assert "清理过期 PIX 链接" in toolbar
    assert "pix_cleanup" in toolbar
    assert "onCleanupExpiredPixLinks" in toolbar
    assert "/tasks/chatgpt/payment-links/pix-cleanup/preview" in page
    assert "/tasks/chatgpt/payment-links/pix-cleanup/task" in page
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
    assert re.search(r"if \(expired <= 0\).*executeExpiredPixLinkCleanup\(\)", page, re.S)

    assert "'pix_cleanup'" in task_modal
    assert "过期 PIX 链接清理" in task_modal
    assert "<TaskLogPanel" in task_modal
    assert "showTaskControls={taskModalMode !== 'pix_cleanup'}" in task_modal
    assert "onTaskDone" in task_modal
