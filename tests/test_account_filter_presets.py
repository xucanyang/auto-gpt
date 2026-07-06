import pytest
from fastapi import HTTPException

from api import accounts
from core.shared_config import LOCAL_ONLY_KEYS


class DummyConfigStore:
    def __init__(self):
        self.value = ""

    def get(self, key: str, default: str = "") -> str:
        assert key == accounts.ACCOUNT_FILTER_PRESETS_CONFIG_KEY
        return self.value or default

    def set(self, key: str, value: str) -> None:
        assert key == accounts.ACCOUNT_FILTER_PRESETS_CONFIG_KEY
        self.value = value


def test_account_filter_presets_are_instance_local():
    assert accounts.ACCOUNT_FILTER_PRESETS_CONFIG_KEY in LOCAL_ONLY_KEYS


def test_account_filter_preset_crud_and_normalization(monkeypatch):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)

    initial = accounts.list_account_filter_presets()
    assert initial["built_in_count"] >= 6
    assert initial["custom_count"] == 0

    created = accounts.create_account_filter_preset(
        accounts.AccountFilterPresetBody(
            name="Plus 长效未上传 OAIPay",
            description="当前筛选组合",
            pinned=True,
            filters={
                "search": " user@example.com ",
                "status": "registered,subscribed",
                "columnFilters": {
                    "subscriptionType": ["plus", "pro", "plus"],
                    "authType": ["refresh_token"],
                    "accountValidity": ["valid"],
                    "oaipayState": ["unknown", "not_found"],
                },
                "sortOrder": "asc",
                "pageSize": 50,
            },
        )
    )
    item = created["item"]
    assert item["built_in"] is False
    assert item["filters"]["search"] == "user@example.com"
    assert item["filters"]["status"] == ["registered", "subscribed"]
    assert item["filters"]["columnFilters"]["subscriptionType"] == ["plus", "pro"]
    assert item["filters"]["sortOrder"] == "asc"
    assert item["filters"]["pageSize"] == 50
    assert created["custom_count"] == 1

    with pytest.raises(HTTPException) as duplicate:
        accounts.create_account_filter_preset(
            accounts.AccountFilterPresetBody(name="Plus 长效未上传 OAIPay", filters={})
        )
    assert duplicate.value.status_code == 400

    updated = accounts.update_account_filter_preset(
        item["id"],
        accounts.AccountFilterPresetBody(
            name="Plus 长效待补传",
            description="改名",
            pinned=False,
            filters={"columnFilters": {"oaipayState": ["deleted_exact_match"]}, "pageSize": 999},
        ),
    )
    assert updated["item"]["name"] == "Plus 长效待补传"
    assert updated["item"]["filters"]["columnFilters"]["oaipayState"] == ["deleted_exact_match"]
    assert updated["item"]["filters"]["pageSize"] == 20

    deleted = accounts.delete_account_filter_preset(item["id"])
    assert deleted["custom_count"] == 0


def test_builtin_filter_preset_cannot_be_mutated(monkeypatch):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)

    with pytest.raises(HTTPException) as update_error:
        accounts.update_account_filter_preset(
            "builtin_oaipay_pending",
            accounts.AccountFilterPresetBody(name="x", filters={}),
        )
    assert update_error.value.status_code == 400

    with pytest.raises(HTTPException) as delete_error:
        accounts.delete_account_filter_preset("builtin_oaipay_pending")
    assert delete_error.value.status_code == 400
