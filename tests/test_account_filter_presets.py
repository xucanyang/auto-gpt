import json

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine

from api import accounts
from core.db import AccountModel
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
    assert initial["built_in_count"] >= 5
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
                    "phoneBindingState": ["unbound"],
                    "paymentLinkPlatform": ["has_link", "pix", "none", "pix"],
                    "paymentLinkGenerated": ["succeeded", "true"],
                    "accountValidity": ["valid"],
                    "oaipayState": ["unknown", "not_found"],
                    "ideaSubmitState": ["available", "submitted", "processing", "unavailable"],
                    "submitState": ["available", "submitted", "processing", "timeout"],
                    "hasSubmitted": ["true"],
                },
                "sortOrder": "asc",
                "pageSize": 50,
            },
        )
    )
    item = created["item"]
    assert item["built_in"] is False
    assert item["mode"] == "dynamic"
    assert item["account_ids"] == []
    assert item["account_count"] == 0
    assert item["filters"]["search"] == "user@example.com"
    assert item["filters"]["status"] == ["registered", "subscribed"]
    assert item["filters"]["columnFilters"]["subscriptionType"] == ["plus", "pro"]
    assert item["filters"]["columnFilters"]["phoneBindingState"] == ["unbound"]
    assert item["filters"]["columnFilters"]["paymentLinkPlatform"] == ["has_link", "pix", "none"]
    assert item["filters"]["columnFilters"]["paymentLinkGenerated"] == ["true"]
    assert item["filters"]["columnFilters"]["oaipayState"] == ["not_uploaded"]
    assert item["filters"]["columnFilters"]["ideaSubmitState"] == ["unsubmitted", "submitting", "unavailable"]
    assert item["filters"]["columnFilters"]["submitState"] == ["unsubmitted", "submitting", "timeout"]
    assert item["filters"]["columnFilters"]["hasSubmitted"] == ["true"]
    assert item["filters"]["sortOrder"] == "asc"
    assert item["filters"]["registrationSortOrder"] == "desc"
    assert item["filters"]["pageSize"] == 50
    assert created["custom_count"] == 1

    normalized_all_history = accounts._normalize_filter_preset_filters(
        {"columnFilters": {"paymentLinkGenerated": ["true", "never"]}}
    )
    assert normalized_all_history["columnFilters"]["paymentLinkGenerated"] == []
    assert normalized_all_history["registrationSortOrder"] == "desc"

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
    assert updated["item"]["filters"]["columnFilters"]["oaipayState"] == ["not_uploaded"]
    assert updated["item"]["filters"]["pageSize"] == 20

    deleted = accounts.delete_account_filter_preset(item["id"])
    assert deleted["custom_count"] == 0


def test_builtin_filter_preset_can_be_updated_and_deleted(monkeypatch):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)

    initial = accounts.list_account_filter_presets()
    initial_builtin_count = initial["built_in_count"]

    updated = accounts.update_account_filter_preset(
        "builtin_oaipay_pending",
        accounts.AccountFilterPresetBody(
            name="OAIPay 待补传（改）",
            description="允许直接调整内置组合",
            pinned=False,
            filters={"columnFilters": {"oaipayState": ["not_found"]}, "pageSize": 50},
        ),
    )
    assert updated["item"]["id"] == "builtin_oaipay_pending"
    assert updated["item"]["built_in"] is True
    assert updated["item"]["name"] == "OAIPay 待补传（改）"
    assert updated["item"]["pinned"] is False
    assert updated["item"]["filters"]["columnFilters"]["oaipayState"] == ["not_uploaded"]
    assert updated["built_in_count"] == initial_builtin_count
    assert updated["builtin_override_count"] == 1

    reloaded = accounts.list_account_filter_presets()
    builtin = next(item for item in reloaded["items"] if item["id"] == "builtin_oaipay_pending")
    assert builtin["name"] == "OAIPay 待补传（改）"

    deleted = accounts.delete_account_filter_preset("builtin_oaipay_pending")
    assert deleted["built_in_count"] == initial_builtin_count - 1
    assert deleted["deleted_builtin_count"] == 1
    assert all(item["id"] != "builtin_oaipay_pending" for item in deleted["items"])

    with pytest.raises(HTTPException) as delete_error:
        accounts.delete_account_filter_preset("builtin_oaipay_pending")
    assert delete_error.value.status_code == 404


def test_legacy_filter_preset_list_payload_still_loads(monkeypatch):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)
    store.value = (
        '[{"id":"preset_legacy","name":"旧自定义组合","filters":{"columnFilters":{"oaipayState":["unknown"],"ideaSubmitState":["submitted","unavailable"]}}}]'
    )

    listed = accounts.list_account_filter_presets()
    assert listed["custom_count"] == 1
    item = next(item for item in listed["items"] if item["id"] == "preset_legacy")
    assert item["mode"] == "dynamic"
    assert item["account_ids"] == []
    assert item["filters"]["columnFilters"]["ideaSubmitState"] == ["submitting", "unavailable"]
    assert item["filters"]["columnFilters"]["oaipayState"] == ["not_uploaded"]
    assert item["filters"]["registrationSortOrder"] == "desc"


def test_filter_preset_v2_registration_default_migrates_once():
    legacy = accounts._normalize_filter_preset_state(
        {
            "version": 2,
            "custom": [
                {
                    "id": "preset_v2",
                    "name": "旧默认排序",
                    "filters": {"registrationSortOrder": "asc"},
                }
            ],
        }
    )
    current = accounts._normalize_filter_preset_state(
        {
            "version": accounts.ACCOUNT_FILTER_PRESET_SCHEMA_VERSION,
            "custom": [
                {
                    "id": "preset_v3",
                    "name": "显式最早排序",
                    "filters": {"registrationSortOrder": "asc"},
                }
            ],
        }
    )

    assert legacy["version"] == accounts.ACCOUNT_FILTER_PRESET_SCHEMA_VERSION
    assert legacy["custom"][0]["filters"]["registrationSortOrder"] == "desc"
    assert current["custom"][0]["filters"]["registrationSortOrder"] == "asc"
    assert "注册排序=最早" in current["custom"][0]["summary"]


def test_fixed_account_filter_preset_persists_members_and_reports_deleted_accounts(monkeypatch, tmp_path):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)
    engine = create_engine(f"sqlite:///{tmp_path / 'fixed-presets.db'}")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        first = AccountModel(platform="chatgpt", email="fixed-a@example.com", password="pw")
        second = AccountModel(platform="chatgpt", email="fixed-b@example.com", password="pw")
        foreign = AccountModel(platform="other", email="foreign@example.com", password="pw")
        session.add(first)
        session.add(second)
        session.add(foreign)
        session.commit()
        session.refresh(first)
        session.refresh(second)
        session.refresh(foreign)
        first_id = int(first.id)
        second_id = int(second.id)
        foreign_id = int(foreign.id)

        created = accounts.create_account_filter_preset(
            accounts.AccountFilterPresetBody(
                name="人工特选号",
                description="固定成员",
                mode="fixed",
                account_ids=[second_id, first_id, second_id, foreign_id, 999999, -1],
                filters={"status": ["subscribed"]},
                pinned=True,
            ),
            session=session,
        )
        item = created["item"]
        assert item["mode"] == "fixed"
        assert item["account_ids"] == [second_id, first_id]
        assert "account_refs" not in item
        stored_item = json.loads(store.value)["custom"][0]
        assert [ref["id"] for ref in stored_item["account_refs"]] == [second_id, first_id]
        assert [ref["email"] for ref in stored_item["account_refs"]] == [
            "fixed-b@example.com",
            "fixed-a@example.com",
        ]
        assert item["account_count"] == 2
        assert item["summary"] == "固定账号 · 2 个"
        assert item["filters"] == accounts._empty_filter_preset_payload()
        assert created["discarded_account_ids"] == [foreign_id, 999999]
        assert created["fixed_count"] == 1
        preset_id = item["id"]

        session.expire_all()
        listed = accounts.list_accounts(
            platform="chatgpt",
            filter_preset_id=preset_id,
            session=session,
        )
        assert listed["total"] == 2
        assert {row["id"] for row in listed["items"]} == {first_id, second_id}
        assert listed["fixed_preset"] == {
            "id": preset_id,
            "stored_account_count": 2,
            "resolved_account_ids": [second_id, first_id],
            "missing_account_ids": [],
        }

        session.delete(second)
        session.commit()
        replacement = AccountModel(
            id=second_id,
            platform="chatgpt",
            email="replacement@example.com",
            password="pw",
        )
        session.add(replacement)
        session.commit()
        session.expire_all()
        listed_after_delete = accounts.list_accounts(
            platform="chatgpt",
            filter_preset_id=preset_id,
            session=session,
        )
        assert listed_after_delete["total"] == 1
        assert [row["id"] for row in listed_after_delete["items"]] == [first_id]
        assert listed_after_delete["fixed_preset"]["resolved_account_ids"] == [first_id]
        assert listed_after_delete["fixed_preset"]["missing_account_ids"] == [second_id]

        session.delete(first)
        session.commit()
        listed_after_all_members_deleted = accounts.list_accounts(
            platform="chatgpt",
            filter_preset_id=preset_id,
            session=session,
        )
        assert listed_after_all_members_deleted["total"] == 0
        assert listed_after_all_members_deleted["items"] == []
        assert listed_after_all_members_deleted["fixed_preset"]["resolved_account_ids"] == []
        assert listed_after_all_members_deleted["fixed_preset"]["missing_account_ids"] == [
            second_id,
            first_id,
        ]


def test_fixed_account_filter_preset_requires_members_and_enforces_limit(monkeypatch, tmp_path):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)
    engine = create_engine(f"sqlite:///{tmp_path / 'fixed-presets-validation.db'}")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        with pytest.raises(HTTPException) as empty_error:
            accounts.create_account_filter_preset(
                accounts.AccountFilterPresetBody(name="空组合", mode="fixed", account_ids=[]),
                session=session,
            )
        assert empty_error.value.status_code == 400

        with pytest.raises(HTTPException) as limit_error:
            accounts.create_account_filter_preset(
                accounts.AccountFilterPresetBody(
                    name="超限组合",
                    mode="fixed",
                    account_ids=list(range(1, accounts.ACCOUNT_FILTER_PRESET_MAX_ACCOUNT_IDS + 2)),
                ),
                session=session,
            )
        assert limit_error.value.status_code == 400
