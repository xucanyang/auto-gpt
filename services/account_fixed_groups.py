from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func
from sqlmodel import Session, select

from core.db import AccountFixedGroupMemberModel, AccountFixedGroupModel, AccountModel


class FixedGroupConflictError(ValueError):
    def __init__(self, conflicts: list[dict[str, Any]]):
        self.conflicts = conflicts
        super().__init__("所选账号已属于其他固定账号组合")


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def account_created_at_identity(value: Any) -> str:
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(timezone.utc).replace(tzinfo=None)
        return normalized.isoformat(timespec="microseconds")
    return str(value or "").strip()


def normalize_account_ids(values: Iterable[Any] | None, *, limit: int = 5000) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for raw in values or []:
        if isinstance(raw, bool):
            continue
        try:
            account_id = int(raw or 0)
        except (TypeError, ValueError):
            continue
        if account_id <= 0 or account_id in seen:
            continue
        seen.add(account_id)
        result.append(account_id)
        if len(result) >= limit:
            break
    return result


def get_fixed_group(session: Session, group_id: str) -> AccountFixedGroupModel | None:
    normalized_id = str(group_id or "").strip()
    if not normalized_id:
        return None
    return session.get(AccountFixedGroupModel, normalized_id)


def list_fixed_groups(
    session: Session,
    *,
    parent_preset_id: str | None = None,
) -> list[AccountFixedGroupModel]:
    query = select(AccountFixedGroupModel)
    normalized_parent = str(parent_preset_id or "").strip()
    if normalized_parent:
        query = query.where(AccountFixedGroupModel.parent_preset_id == normalized_parent)
    query = query.order_by(
        AccountFixedGroupModel.pinned.desc(),
        AccountFixedGroupModel.updated_at.desc(),
        AccountFixedGroupModel.created_at.asc(),
    )
    return list(session.exec(query).all())


def fixed_group_name_exists(
    session: Session,
    *,
    parent_preset_id: str,
    name: str,
    ignore_group_id: str = "",
) -> bool:
    query = select(AccountFixedGroupModel.id).where(
        AccountFixedGroupModel.parent_preset_id == str(parent_preset_id or "").strip(),
        func.lower(AccountFixedGroupModel.name) == str(name or "").strip().lower(),
    )
    normalized_ignore = str(ignore_group_id or "").strip()
    if normalized_ignore:
        query = query.where(AccountFixedGroupModel.id != normalized_ignore)
    return session.exec(query).first() is not None


def create_fixed_group(
    session: Session,
    *,
    group_id: str,
    parent_preset_id: str,
    name: str,
    description: str = "",
    pinned: bool = True,
) -> AccountFixedGroupModel:
    now = utc_iso()
    group = AccountFixedGroupModel(
        id=str(group_id or "").strip(),
        parent_preset_id=str(parent_preset_id or "").strip(),
        name=str(name or "").strip(),
        description=str(description or "").strip(),
        pinned=bool(pinned),
        revision=1,
        created_at=now,
        updated_at=now,
    )
    session.add(group)
    session.flush()
    return group


def update_fixed_group_meta(
    session: Session,
    group: AccountFixedGroupModel,
    *,
    name: str,
    description: str,
    pinned: bool,
) -> AccountFixedGroupModel:
    group.name = str(name or "").strip()
    group.description = str(description or "").strip()
    group.pinned = bool(pinned)
    group.updated_at = utc_iso()
    session.add(group)
    session.flush()
    return group


def _member_rows_by_account_id(
    session: Session,
    account_ids: list[int],
) -> dict[int, AccountFixedGroupMemberModel]:
    if not account_ids:
        return {}
    rows = session.exec(
        select(AccountFixedGroupMemberModel).where(
            AccountFixedGroupMemberModel.account_id.in_(account_ids)
        )
    ).all()
    return {int(row.account_id): row for row in rows}


def replace_fixed_group_members(
    session: Session,
    group: AccountFixedGroupModel,
    account_ids: Iterable[Any],
    *,
    move_conflicts: bool = False,
    max_members: int = 5000,
) -> dict[str, Any]:
    requested_ids = normalize_account_ids(account_ids, limit=max_members)
    if not requested_ids:
        raise ValueError("固定账号组合至少需要一个有效账号")

    accounts = session.exec(
        select(AccountModel).where(
            AccountModel.platform == "chatgpt",
            AccountModel.id.in_(requested_ids),
        )
    ).all()
    accounts_by_id = {
        int(account.id): account
        for account in accounts
        if int(account.id or 0) > 0
    }
    resolved_ids = [account_id for account_id in requested_ids if account_id in accounts_by_id]
    discarded_ids = [account_id for account_id in requested_ids if account_id not in accounts_by_id]
    if not resolved_ids:
        raise ValueError("所选账号已不存在，无法保存固定账号组合")

    existing_members = _member_rows_by_account_id(session, resolved_ids)
    conflicting_ids = [
        account_id
        for account_id in resolved_ids
        if account_id in existing_members
        and existing_members[account_id].fixed_group_id != group.id
    ]
    if conflicting_ids and not move_conflicts:
        conflicting_group_ids = sorted({existing_members[account_id].fixed_group_id for account_id in conflicting_ids})
        group_rows = session.exec(
            select(AccountFixedGroupModel).where(AccountFixedGroupModel.id.in_(conflicting_group_ids))
        ).all()
        group_names = {row.id: row.name for row in group_rows}
        conflicts = [
            {
                "account_id": account_id,
                "fixed_group_id": existing_members[account_id].fixed_group_id,
                "fixed_group_name": group_names.get(existing_members[account_id].fixed_group_id, ""),
            }
            for account_id in conflicting_ids
        ]
        raise FixedGroupConflictError(conflicts)

    requested_set = set(resolved_ids)
    current_rows = session.exec(
        select(AccountFixedGroupMemberModel).where(
            AccountFixedGroupMemberModel.fixed_group_id == group.id
        )
    ).all()
    removed_ids: list[int] = []
    for row in current_rows:
        account_id = int(row.account_id)
        if account_id in requested_set:
            continue
        removed_ids.append(account_id)
        session.delete(row)

    now = utc_iso()
    moved_ids: list[int] = []
    added_ids: list[int] = []
    rebound_ids: list[int] = []
    for account_id in resolved_ids:
        account = accounts_by_id[account_id]
        account_email = str(account.email or "").strip().lower()
        account_created_at = account_created_at_identity(account.created_at)
        row = existing_members.get(account_id)
        if row is None:
            row = AccountFixedGroupMemberModel(
                account_id=account_id,
                fixed_group_id=group.id,
                account_email=account_email,
                account_created_at=account_created_at,
                assigned_at=now,
            )
            added_ids.append(account_id)
        else:
            if row.fixed_group_id != group.id:
                moved_ids.append(account_id)
            identity_changed = (
                str(row.account_email or "").strip().lower() != account_email
                or str(row.account_created_at or "").strip() != account_created_at
            )
            if identity_changed:
                rebound_ids.append(account_id)
            if row.fixed_group_id != group.id or identity_changed:
                row.fixed_group_id = group.id
                row.account_email = account_email
                row.account_created_at = account_created_at
                row.assigned_at = now
        session.add(row)

    if added_ids or moved_ids or removed_ids or rebound_ids:
        group.revision = max(1, int(group.revision or 1)) + 1
        group.updated_at = now
        session.add(group)
    session.flush()
    return {
        "account_ids": resolved_ids,
        "discarded_account_ids": discarded_ids,
        "added_account_ids": added_ids,
        "moved_account_ids": moved_ids,
        "removed_account_ids": removed_ids,
        "rebound_account_ids": rebound_ids,
    }


def fixed_group_members(
    session: Session,
    group_id: str,
) -> list[tuple[AccountFixedGroupMemberModel, AccountModel]]:
    rows = session.exec(
        select(AccountFixedGroupMemberModel, AccountModel)
        .join(AccountModel, AccountModel.id == AccountFixedGroupMemberModel.account_id)
        .where(
            AccountFixedGroupMemberModel.fixed_group_id == str(group_id or "").strip(),
            AccountModel.platform == "chatgpt",
        )
        .order_by(AccountFixedGroupMemberModel.assigned_at.asc(), AccountModel.id.asc())
    ).all()
    return [
        (member, account)
        for member, account in rows
        if str(account.email or "").strip().lower() == str(member.account_email or "").strip().lower()
        and account_created_at_identity(account.created_at) == str(member.account_created_at or "").strip()
    ]


def fixed_group_member_ids(session: Session, group_id: str) -> list[int]:
    return [int(account.id) for _, account in fixed_group_members(session, group_id)]


def serialize_fixed_group(
    session: Session,
    group: AccountFixedGroupModel,
    *,
    include_account_ids: bool = True,
) -> dict[str, Any]:
    member_ids = fixed_group_member_ids(session, group.id)
    return {
        "id": group.id,
        "parent_preset_id": group.parent_preset_id,
        "name": group.name,
        "description": group.description,
        "mode": "fixed",
        "account_ids": member_ids if include_account_ids else [],
        "account_count": len(member_ids),
        "summary": "固定账号",
        "pinned": bool(group.pinned),
        "built_in": False,
        "revision": int(group.revision or 1),
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


def delete_fixed_group(session: Session, group: AccountFixedGroupModel) -> list[int]:
    rows = session.exec(
        select(AccountFixedGroupMemberModel).where(
            AccountFixedGroupMemberModel.fixed_group_id == group.id
        )
    ).all()
    released_ids = [int(row.account_id) for row in rows]
    for row in rows:
        session.delete(row)
    session.delete(group)
    session.flush()
    return released_ids
