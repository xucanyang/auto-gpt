import pytest

from api import config as config_api
from core.shared_config import filter_shareable_config, is_shareable_key


class _FakeConfigStore:
    def __init__(self):
        self.calls = []
        self.pushed = None

    def get_saved_local_all(self):
        return {
            "mail_provider": "tempmail_lol",
            "auth_password_hash": "must-stay-local",
            "_config_share_enabled": "false",
        }

    def push_to_shared(self, data, *, replace, base_revision, action, note):
        self.calls.append(("push", replace, base_revision, action, note))
        self.pushed = dict(data)
        return {"ok": True, "revision": 12}

    def enable_shared(self, *, pull):
        self.calls.append(("enable", pull))
        return {"enabled": True, "mode": "shared", "shared": {"revision": 12}}

    def get_share_state(self):
        self.calls.append(("state",))
        return {"enabled": False, "mode": "local", "shared": {"revision": 12}}


def test_local_publish_can_attach_current_instance(monkeypatch):
    store = _FakeConfigStore()
    monkeypatch.setattr(config_api, "config_store", store)

    result = config_api.push_instance_config_to_shared(
        config_api.SharePushRequest(
            confirm=True,
            base_revision=11,
            note="test-publish",
            enable_shared=True,
        )
    )

    assert store.pushed == {"mail_provider": "tempmail_lol"}
    assert store.calls == [("push", True, 11, "push", "test-publish"), ("enable", False)]
    assert result["state"]["enabled"] is True
    assert result["state"]["shared"]["revision"] == 12


def test_push_api_remains_push_only_by_default(monkeypatch):
    store = _FakeConfigStore()
    monkeypatch.setattr(config_api, "config_store", store)

    result = config_api.push_instance_config_to_shared(
        config_api.SharePushRequest(confirm=True)
    )

    assert store.calls == [("push", True, None, "push", "instance-push"), ("state",)]
    assert result["state"]["mode"] == "local"


def test_push_requires_explicit_confirmation():
    with pytest.raises(config_api.HTTPException) as exc_info:
        config_api.push_instance_config_to_shared(config_api.SharePushRequest())

    assert exc_info.value.status_code == 400


def test_runtime_capacity_keys_never_enter_shared_template():
    assert not is_shareable_key("chatgpt_runtime_solver_max_browsers")
    assert filter_shareable_config(
        {
            "mail_provider": "tempmail_lol",
            "chatgpt_runtime_auth_browser_max_concurrency": "10",
            "chatgpt_runtime_solver_max_browsers": "5",
        }
    ) == {"mail_provider": "tempmail_lol"}
