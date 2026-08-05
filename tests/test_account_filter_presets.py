import json

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

from api import accounts
from core.db import AccountFixedGroupMemberModel, AccountModel
from core.shared_config import LOCAL_ONLY_KEYS
from services import account_filters


class DummyConfigStore:
    def __init__(self):
        self.value = ""

    def get(self, key: str, default: str = "") -> str:
        assert key == accounts.ACCOUNT_FILTER_PRESETS_CONFIG_KEY
        return self.value or default

    def set(self, key: str, value: str) -> None:
        assert key == accounts.ACCOUNT_FILTER_PRESETS_CONFIG_KEY
        self.value = value


def _test_engine(tmp_path, name: str):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    SQLModel.metadata.create_all(engine)
    return engine


def _create_dynamic_preset(
    session: Session,
    name: str = "全部候选",
    filters: dict | None = None,
) -> dict:
    return accounts.create_account_filter_preset(
        accounts.AccountFilterPresetBody(
            name=name,
            mode="dynamic",
            filters=filters or {},
            pinned=True,
        ),
        session=session,
    )["item"]


def test_account_filter_presets_are_instance_local():
    assert accounts.ACCOUNT_FILTER_PRESETS_CONFIG_KEY in LOCAL_ONLY_KEYS


def test_account_filter_preset_crud_and_normalization(monkeypatch, tmp_path):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)
    engine = _test_engine(tmp_path, "dynamic-presets.db")

    with Session(engine) as session:
        initial = accounts.list_account_filter_presets(session=session)
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
            ),
            session=session,
        )
        item = created["item"]
        assert item["mode"] == "dynamic"
        assert item["filters"]["search"] == "user@example.com"
        assert item["filters"]["status"] == ["registered", "subscribed"]
        assert item["filters"]["columnFilters"]["subscriptionType"] == ["plus", "pro"]
        assert item["filters"]["columnFilters"]["paymentLinkGenerated"] == ["true"]
        assert item["filters"]["columnFilters"]["oaipayState"] == ["not_uploaded"]
        assert item["filters"]["columnFilters"]["submitState"] == ["unsubmitted", "submitting", "timeout"]
        assert item["filters"]["registrationSortOrder"] == "desc"
        assert created["custom_count"] == 1

        with pytest.raises(HTTPException) as duplicate:
            accounts.create_account_filter_preset(
                accounts.AccountFilterPresetBody(name="Plus 长效未上传 OAIPay", filters={}),
                session=session,
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
            session=session,
        )
        assert updated["item"]["name"] == "Plus 长效待补传"
        assert updated["item"]["filters"]["columnFilters"]["oaipayState"] == ["not_uploaded"]
        assert updated["item"]["filters"]["pageSize"] == 20

        deleted = accounts.delete_account_filter_preset(item["id"], session=session)
        assert deleted["custom_count"] == 0


def test_builtin_filter_preset_can_be_updated_and_deleted(monkeypatch, tmp_path):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)
    engine = _test_engine(tmp_path, "builtin-presets.db")

    with Session(engine) as session:
        initial = accounts.list_account_filter_presets(session=session)
        initial_builtin_count = initial["built_in_count"]
        updated = accounts.update_account_filter_preset(
            "builtin_oaipay_pending",
            accounts.AccountFilterPresetBody(
                name="OAIPay 待补传（改）",
                description="允许直接调整内置组合",
                pinned=False,
                filters={"columnFilters": {"oaipayState": ["not_found"]}, "pageSize": 50},
            ),
            session=session,
        )
        assert updated["item"]["built_in"] is True
        assert updated["item"]["name"] == "OAIPay 待补传（改）"
        assert updated["builtin_override_count"] == 1

        reloaded = accounts.list_account_filter_presets(session=session)
        builtin = next(item for item in reloaded["dynamic_items"] if item["id"] == "builtin_oaipay_pending")
        assert builtin["name"] == "OAIPay 待补传（改）"

        deleted = accounts.delete_account_filter_preset("builtin_oaipay_pending", session=session)
        assert deleted["built_in_count"] == initial_builtin_count - 1
        with pytest.raises(HTTPException) as delete_error:
            accounts.delete_account_filter_preset("builtin_oaipay_pending", session=session)
        assert delete_error.value.status_code == 404


def test_legacy_filter_preset_payload_still_loads(monkeypatch, tmp_path):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)
    store.value = '[{"id":"preset_legacy","name":"旧自定义组合","filters":{"columnFilters":{"oaipayState":["unknown"],"ideaSubmitState":["submitted","unavailable"]}}}]'
    engine = _test_engine(tmp_path, "legacy-presets.db")

    with Session(engine) as session:
        listed = accounts.list_account_filter_presets(session=session)
        item = next(item for item in listed["dynamic_items"] if item["id"] == "preset_legacy")
        assert item["mode"] == "dynamic"
        assert item["filters"]["columnFilters"]["ideaSubmitState"] == ["submitting", "unavailable"]
        assert item["filters"]["columnFilters"]["oaipayState"] == ["not_uploaded"]


def test_filter_preset_v2_registration_default_migrates_once():
    legacy = accounts._normalize_filter_preset_state(
        {"version": 2, "custom": [{"id": "preset_v2", "name": "旧默认排序", "filters": {"registrationSortOrder": "asc"}}]}
    )
    current = accounts._normalize_filter_preset_state(
        {
            "version": accounts.ACCOUNT_FILTER_PRESET_SCHEMA_VERSION,
            "custom": [{"id": "preset_v3", "name": "显式最早排序", "filters": {"registrationSortOrder": "asc"}}],
        }
    )
    assert legacy["custom"][0]["filters"]["registrationSortOrder"] == "desc"
    assert current["custom"][0]["filters"]["registrationSortOrder"] == "asc"


def test_fixed_groups_are_parented_exclusive_and_separate_from_selection(monkeypatch, tmp_path):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)
    engine = _test_engine(tmp_path, "fixed-groups.db")

    with Session(engine) as session:
        first = AccountModel(platform="chatgpt", email="fixed-a@example.com", password="pw")
        second = AccountModel(platform="chatgpt", email="fixed-b@example.com", password="pw")
        third = AccountModel(platform="chatgpt", email="fixed-c@example.com", password="pw")
        foreign = AccountModel(platform="other", email="foreign@example.com", password="pw")
        session.add_all([first, second, third, foreign])
        session.commit()
        for item in (first, second, third, foreign):
            session.refresh(item)

        parent = _create_dynamic_preset(session)
        created = accounts.create_account_filter_preset(
            accounts.AccountFilterPresetBody(
                name="人工特选号",
                description="固定成员",
                mode="fixed",
                parent_preset_id=parent["id"],
                account_ids=[int(second.id), int(first.id), int(second.id), int(foreign.id), 999999],
                pinned=True,
            ),
            session=session,
        )
        group = created["item"]
        assert group["mode"] == "fixed"
        assert group["parent_preset_id"] == parent["id"]
        assert set(group["account_ids"]) == {int(first.id), int(second.id)}
        assert created["discarded_account_ids"] == [int(foreign.id), 999999]
        assert json.loads(store.value)["custom"][0]["id"] == parent["id"]
        assert len(session.exec(select(AccountFixedGroupMemberModel)).all()) == 2

        fixed_view = accounts.list_accounts(
            platform="chatgpt",
            primary_preset_id=parent["id"],
            secondary_scope="fixed",
            fixed_group_id=group["id"],
            fixed_group_revision=group["revision"],
            session=session,
        )
        assert fixed_view["total"] == 2
        assert {row["id"] for row in fixed_view["items"]} == {int(first.id), int(second.id)}

        unassigned_view = accounts.list_accounts(
            platform="chatgpt",
            primary_preset_id=parent["id"],
            secondary_scope="unassigned",
            session=session,
        )
        assert unassigned_view["total"] == 1
        assert [row["id"] for row in unassigned_view["items"]] == [int(third.id)]

        # A condition-based ChatGPT query without an explicit secondary scope
        # still belongs to the unassigned pool and cannot absorb fixed members.
        unscoped_view = accounts.list_accounts(
            platform="chatgpt",
            status="registered",
            session=session,
        )
        assert unscoped_view["total"] == 1
        assert [row["id"] for row in unscoped_view["items"]] == [int(third.id)]
        task_scope = account_filters.resolve_filtered_accounts(
            session,
            platform="chatgpt",
            filter_source={"status": "registered"},
        )
        assert task_scope.account_ids == (int(third.id),)

        other_parent = _create_dynamic_preset(session, name="第二条件组合")
        with pytest.raises(HTTPException) as conflict:
            accounts.create_account_filter_preset(
                accounts.AccountFilterPresetBody(
                    name="第二组",
                    mode="fixed",
                    parent_preset_id=other_parent["id"],
                    account_ids=[int(first.id)],
                ),
                session=session,
            )
        assert conflict.value.status_code == 409
        assert conflict.value.detail["code"] == "FIXED_GROUP_MEMBER_CONFLICT"


def test_fixed_group_update_validates_only_new_parent_members_and_revision(monkeypatch, tmp_path):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)
    engine = _test_engine(tmp_path, "fixed-update.db")

    with Session(engine) as session:
        member = AccountModel(
            platform="chatgpt",
            email="member@example.com",
            password="pw",
            status="registered",
        )
        outside = AccountModel(
            platform="chatgpt",
            email="outside@example.com",
            password="pw",
            status="invalid",
        )
        new_member = AccountModel(
            platform="chatgpt",
            email="new-member@example.com",
            password="pw",
            status="registered",
        )
        session.add_all([member, outside, new_member])
        session.commit()
        session.refresh(member)
        session.refresh(outside)
        session.refresh(new_member)
        parent = _create_dynamic_preset(
            session,
            name="仅已注册",
            filters={"status": ["registered"]},
        )
        group = accounts.create_account_filter_preset(
            accounts.AccountFilterPresetBody(
                name="稳定固定组",
                mode="fixed",
                parent_preset_id=parent["id"],
                account_ids=[int(member.id)],
            ),
            session=session,
        )["item"]

        # Membership is stable after assignment: later status drift does not
        # eject the account or prevent a metadata-only edit.
        member.status = "invalid"
        session.add(member)
        session.commit()
        updated = accounts.update_account_filter_preset(
            group["id"],
            accounts.AccountFilterPresetBody(
                name="稳定固定组（改）",
                mode="fixed",
                parent_preset_id=parent["id"],
                account_ids=[int(member.id)],
            ),
            session=session,
        )["item"]
        assert updated["revision"] == group["revision"]
        fixed_view = accounts.list_accounts(
            platform="chatgpt",
            primary_preset_id=parent["id"],
            secondary_scope="fixed",
            fixed_group_id=group["id"],
            fixed_group_revision=updated["revision"],
            session=session,
        )
        assert [row["id"] for row in fixed_view["items"]] == [int(member.id)]

        # A newly added member must still satisfy the parent at assignment time.
        with pytest.raises(HTTPException) as outside_error:
            accounts.update_account_filter_preset(
                group["id"],
                accounts.AccountFilterPresetBody(
                    name="稳定固定组（改）",
                    mode="fixed",
                    parent_preset_id=parent["id"],
                    account_ids=[int(member.id), int(outside.id)],
                ),
                session=session,
            )
        assert outside_error.value.status_code == 409
        assert outside_error.value.detail["code"] == "FIXED_GROUP_PARENT_SCOPE_CHANGED"

        request = account_filters.AccountFilterRequestMixin(
            primary_preset_id=parent["id"],
            secondary_scope="fixed",
            fixed_group_id=group["id"],
            fixed_group_revision=updated["revision"],
            expected_total=1,
        )
        resolution = account_filters.resolve_filtered_accounts(
            session,
            platform="chatgpt",
            filter_source=request,
            verify_expected_total=True,
        )
        assert resolution.account_ids == (int(member.id),)

        latest = accounts.update_account_filter_preset(
            group["id"],
            accounts.AccountFilterPresetBody(
                name="稳定固定组（再改）",
                mode="fixed",
                parent_preset_id=parent["id"],
                account_ids=[int(member.id), int(new_member.id)],
            ),
            session=session,
        )["item"]
        assert latest["revision"] > updated["revision"]
        with pytest.raises(account_filters.AccountFixedGroupScopeChangedError):
            account_filters.resolve_filtered_accounts(
                session,
                platform="chatgpt",
                filter_source=request,
                verify_expected_total=True,
            )


def test_fixed_group_identity_does_not_rebind_reused_account_id(monkeypatch, tmp_path):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)
    engine = _test_engine(tmp_path, "fixed-identity.db")

    with Session(engine) as session:
        original = AccountModel(platform="chatgpt", email="original@example.com", password="pw")
        session.add(original)
        session.commit()
        session.refresh(original)
        original_id = int(original.id)
        parent = _create_dynamic_preset(session)
        group = accounts.create_account_filter_preset(
            accounts.AccountFilterPresetBody(
                name="稳定身份",
                mode="fixed",
                parent_preset_id=parent["id"],
                account_ids=[original_id],
            ),
            session=session,
        )["item"]

        session.delete(original)
        session.commit()
        replacement = AccountModel(
            id=original_id,
            platform="chatgpt",
            email="replacement@example.com",
            password="pw",
        )
        session.add(replacement)
        session.commit()

        fixed_view = accounts.list_accounts(
            platform="chatgpt",
            primary_preset_id=parent["id"],
            secondary_scope="fixed",
            fixed_group_id=group["id"],
            session=session,
        )
        assert fixed_view["total"] == 0
        unassigned_view = accounts.list_accounts(
            platform="chatgpt",
            primary_preset_id=parent["id"],
            secondary_scope="unassigned",
            session=session,
        )
        assert [row["id"] for row in unassigned_view["items"]] == [original_id]


def test_legacy_fixed_migration_preview_respects_priority(monkeypatch, tmp_path):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)
    engine = _test_engine(tmp_path, "fixed-migration.db")

    with Session(engine) as session:
        first = AccountModel(platform="chatgpt", email="one@example.com", password="pw")
        second = AccountModel(platform="chatgpt", email="two@example.com", password="pw")
        session.add_all([first, second])
        session.commit()
        session.refresh(first)
        session.refresh(second)
        parent = _create_dynamic_preset(session)
        state = accounts._load_filter_preset_state()
        state["custom"].extend([
            {
                "id": "preset_old_a",
                "name": "旧A",
                "mode": "fixed",
                "account_ids": [int(first.id), int(second.id)],
                "account_refs": [accounts._filter_preset_account_ref(first), accounts._filter_preset_account_ref(second)],
                "pinned": True,
            },
            {
                "id": "preset_old_b",
                "name": "旧B",
                "mode": "fixed",
                "account_ids": [int(second.id)],
                "account_refs": [accounts._filter_preset_account_ref(second)],
                "pinned": True,
            },
        ])
        accounts._save_filter_preset_state(state)

        with pytest.raises(HTTPException) as missing_priority:
            accounts.migrate_legacy_fixed_presets(
                accounts.FixedPresetMigrationBody(
                    parent_by_preset_id={"preset_old_a": parent["id"], "preset_old_b": parent["id"]},
                    commit=False,
                ),
                session=session,
            )
        assert missing_priority.value.status_code == 400

        body = accounts.FixedPresetMigrationBody(
            parent_by_preset_id={"preset_old_a": parent["id"], "preset_old_b": parent["id"]},
            priority_order=["preset_old_b", "preset_old_a"],
            commit=False,
        )
        preview = accounts.migrate_legacy_fixed_presets(body, session=session)["preview"]
        groups = {item["id"]: item for item in preview["groups"]}
        assert groups["preset_old_b"]["assigned_account_count"] == 1
        assert groups["preset_old_a"]["assigned_account_count"] == 1
        assert groups["preset_old_a"]["conflict_account_count"] == 1
        assert preview["conflict_account_count"] == 1


def test_legacy_fixed_migration_blocks_members_outside_selected_parent(monkeypatch, tmp_path):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)
    engine = _test_engine(tmp_path, "fixed-migration-parent.db")

    with Session(engine) as session:
        inside = AccountModel(platform="chatgpt", email="inside@example.com", password="pw", status="registered")
        outside = AccountModel(platform="chatgpt", email="outside@example.com", password="pw", status="invalid")
        session.add_all([inside, outside])
        session.commit()
        session.refresh(inside)
        session.refresh(outside)
        parent = _create_dynamic_preset(
            session,
            name="仅注册账号",
            filters={"status": ["registered"]},
        )
        state = accounts._load_filter_preset_state()
        state["custom"].append(
            {
                "id": "preset_old_parent_mismatch",
                "name": "旧父级不匹配",
                "mode": "fixed",
                "account_ids": [int(inside.id), int(outside.id)],
                "account_refs": [
                    accounts._filter_preset_account_ref(inside),
                    accounts._filter_preset_account_ref(outside),
                ],
                "pinned": True,
            }
        )
        accounts._save_filter_preset_state(state)
        body = accounts.FixedPresetMigrationBody(
            parent_by_preset_id={"preset_old_parent_mismatch": parent["id"]},
            priority_order=["preset_old_parent_mismatch"],
            commit=False,
        )
        preview = accounts.migrate_legacy_fixed_presets(body, session=session)["preview"]
        assert preview["groups"][0]["assigned_account_count"] == 1
        assert preview["groups"][0]["outside_parent_account_count"] == 1
        assert preview["outside_parent_account_count"] == 1

        with pytest.raises(HTTPException) as commit_error:
            accounts.migrate_legacy_fixed_presets(
                body.model_copy(update={"commit": True}),
                session=session,
            )
        assert commit_error.value.status_code == 409
        assert commit_error.value.detail["code"] == "FIXED_GROUP_PARENT_SCOPE_CHANGED"


def test_fixed_group_requires_parent_members_and_limit(monkeypatch, tmp_path):
    store = DummyConfigStore()
    monkeypatch.setattr(accounts, "config_store", store)
    engine = _test_engine(tmp_path, "fixed-validation.db")
    with Session(engine) as session:
        parent = _create_dynamic_preset(session)
        with pytest.raises(HTTPException) as empty_error:
            accounts.create_account_filter_preset(
                accounts.AccountFilterPresetBody(
                    name="空组合",
                    mode="fixed",
                    parent_preset_id=parent["id"],
                    account_ids=[],
                ),
                session=session,
            )
        assert empty_error.value.status_code == 400

        with pytest.raises(HTTPException) as limit_error:
            accounts.create_account_filter_preset(
                accounts.AccountFilterPresetBody(
                    name="超限组合",
                    mode="fixed",
                    parent_preset_id=parent["id"],
                    account_ids=list(range(1, accounts.ACCOUNT_FILTER_PRESET_MAX_ACCOUNT_IDS + 2)),
                ),
                session=session,
            )
        assert limit_error.value.status_code == 400
