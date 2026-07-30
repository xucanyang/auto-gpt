from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from api import accounts
from core.db import AccountListStateModel, AccountModel


def test_batch_delete_deduplicates_ids_and_removes_derived_state(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'accounts.db'}")
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        first = AccountModel(platform="chatgpt", email="batch-a@example.com", password="pw")
        second = AccountModel(platform="chatgpt", email="batch-b@example.com", password="pw")
        session.add(first)
        session.add(second)
        session.flush()
        session.add(AccountListStateModel(account_id=int(first.id), platform="chatgpt"))
        session.add(AccountListStateModel(account_id=int(second.id), platform="chatgpt"))
        session.commit()
        first_id = int(first.id)
        second_id = int(second.id)

    with Session(engine) as session:
        result = accounts.batch_delete_accounts(
            accounts.BatchDeleteRequest(ids=[first_id, first_id, second_id, 999999]),
            session=session,
        )

    assert result == {
        "deleted": 2,
        "not_found": [999999],
        "total_requested": 3,
    }
    with Session(engine) as session:
        assert session.exec(select(AccountModel)).all() == []
        assert session.exec(select(AccountListStateModel)).all() == []
