from pathlib import Path


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


def test_expired_pix_link_cleanup_is_previewed_confirmed_and_refetched():
    page = ACCOUNTS_PAGE.read_text(encoding="utf-8")
    toolbar = ACCOUNTS_TOOLBAR.read_text(encoding="utf-8")

    assert "清理过期 PIX 链接" in toolbar
    assert "pix_cleanup" in toolbar
    assert "onCleanupExpiredPixLinks" in toolbar
    assert "/tasks/chatgpt/payment-links/pix-cleanup/preview" in page
    assert "/tasks/chatgpt/payment-links/pix-cleanup" in page
    assert "只清理账号当前 PIX 链接" in page
    assert "不会删除账号、支付生成历史、PIX CDK 或提交结果" in page
    assert "await accountsQuery.refetch()" in page
