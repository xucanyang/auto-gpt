from core.proxy_utils import is_proxy_error_text


def test_hme_ready_control_plane_timeout_is_not_a_proxy_failure():
    assert not is_proxy_error_text(
        "HME Ready API 调用失败: POST /api/hme-ready/mailboxes/prepare "
        "error=HTTPConnectionPool(host='172.20.0.1', port=18765): Read timed out. (read timeout=20)"
    )


def test_normal_proxy_timeout_remains_a_proxy_failure():
    assert is_proxy_error_text("SOCKSHTTPSConnectionPool: Read timed out")
