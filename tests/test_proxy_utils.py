from core.proxy_utils import is_proxy_error_text


def test_hme_ready_control_plane_timeout_is_not_a_proxy_failure():
    assert not is_proxy_error_text(
        "HME Ready API 调用失败: POST /api/hme-ready/mailboxes/prepare "
        "error=HTTPConnectionPool(host='172.20.0.1', port=18765): Read timed out. (read timeout=20)"
    )


def test_normal_proxy_timeout_remains_a_proxy_failure():
    assert is_proxy_error_text("SOCKSHTTPSConnectionPool: Read timed out")


def test_pool_empty_country_is_not_forced_to_jp(monkeypatch):
    """表单显式提交空国家时，代理池不得回落到全局 JP。"""
    from core import proxy_utils as pu

    captured = {}

    def fake_pool_candidates(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(pu, "_pool_candidate_tuples", fake_pool_candidates)
    monkeypatch.setattr(pu, "_configured_value", lambda key, default="": "JP" if "country" in key else default)
    monkeypatch.setattr(pu, "_global_task_proxy_mode", lambda default="": "pool")
    monkeypatch.setattr(pu, "_is_global_task_proxy_default", lambda default_mode: True)

    try:
        pu.resolve_task_proxy_candidates(
            {
                "proxy_mode": "pool",
                "proxy_country_code": "",
                "proxy_min_score": 50,
                "proxy_max_candidates": 5,
            },
            default_mode="global",
            target="chatgpt",
        )
    except RuntimeError as exc:
        assert "代理池没有可用候选" in str(exc)
        assert "不限" in str(exc) or "country=不限" in str(exc) or "min_score" in str(exc)
    else:
        raise AssertionError("expected empty pool RuntimeError")

    assert captured.get("country_code") == ""


def test_dynamic_empty_country_raises_instead_of_defaulting_jp(monkeypatch):
    from core import proxy_utils as pu

    monkeypatch.setattr(pu, "get_global_dynamic_proxy_country", lambda default="": "")
    monkeypatch.setattr(pu, "get_global_dynamic_proxy_template", lambda: "socks5://u:p@h:1")
    monkeypatch.setattr(pu, "_is_global_task_proxy_default", lambda default_mode: True)

    try:
        pu.resolve_task_proxy_candidates(
            {
                "proxy_mode": "dynamic",
                "proxy_country_code": "",
            },
            default_mode="global",
            target="chatgpt",
        )
        raise AssertionError("expected RuntimeError for empty dynamic country")
    except RuntimeError as exc:
        text = str(exc)
        assert "出口国家" in text or "country" in text.lower()
