from pathlib import Path


ACCOUNTS_PAGE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages" / "Accounts.tsx"


def test_pix_saved_link_upload_mode_is_wired_to_the_batch_submit_contract():
    source = ACCOUNTS_PAGE.read_text(encoding="utf-8")

    assert "pix_submit_mode: 'auto_extract'" in source
    assert "raw.pix_submit_mode === 'user_link' ? 'user_link' : 'auto_extract'" in source
    assert "上传已保存 PIX 链接" in source
    assert "body.pix_submit_mode = pixSubmitMode" in source
    assert "PIX 链接上传" in source
    assert "不读取 Access Token" in source
    assert "任务已创建，任务快照暂不可读；日志面板会自动重试。" in source
    assert "void apiFetch(`/tasks/${taskIdFromResponse}`)" in source
