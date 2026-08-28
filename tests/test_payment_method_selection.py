from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from api import accounts as accounts_api
from core.db import AccountModel, AccountPaymentMethodIndexModel
from services import account_filters


@pytest.fixture
def payment_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'payment-method-selection.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _account(account_id: int, country: str, methods: list[str], *, state: str = "available") -> AccountModel:
    account = AccountModel(
        id=account_id,
        platform="chatgpt",
        email=f"payment-{account_id}@example.com",
        password="",
        updated_at=datetime.now(timezone.utc),
    )
    account.set_extra(
        {
            "chatgpt_payment_methods": {
                "confirmed_state": state,
                "confirmed_at": "2026-08-29T00:00:00Z",
                "evidence": {
                    "country": country,
                    "methods": methods,
                    "methods_display": methods,
                },
            }
        }
    )
    return account


def _filtered_ids(session: Session, selection) -> list[int]:
    query, _, _ = account_filters.account_filtered_query(
        session,
        platform="chatgpt",
        filter_source={"payment_method_selection": selection},
    )
    return [int(account.id) for account in session.exec(query).all()]


def test_country_method_selection_keeps_pairs_and_uses_raw_method_ids(payment_engine):
    with Session(payment_engine) as session:
        session.add(_account(1, "VN", ["card", "link"]))
        session.add(_account(2, "PH", ["gcash"]))
        session.add(_account(3, "VN", ["card"]))
        session.commit()
        account_filters.refresh_account_list_state(session)

        assert _filtered_ids(session, [{"country": "VN", "methods": ["Link"]}]) == [1]
        assert _filtered_ids(session, [{"country": "PH", "methods": ["gcash"]}]) == [2]
        assert _filtered_ids(
            session,
            [
                {"country": "VN", "methods": ["link"]},
                {"country": "PH", "methods": ["gcash"]},
            ],
        ) == [1, 2]


def test_payment_method_catalog_is_complete_and_not_page_scoped(payment_engine, monkeypatch):
    with Session(payment_engine) as session:
        session.add(_account(11, "VN", ["card", "link"]))
        session.add(_account(12, "VN", ["link"]))
        session.add(_account(13, "PH", ["gcash"]))
        session.commit()

        monkeypatch.setattr(accounts_api, "_maybe_reconcile_rate_limited_accounts", lambda *args, **kwargs: None)
        response = accounts_api.list_accounts(
            platform="chatgpt",
            page=1,
            page_size=1,
            session=session,
        )

        countries = {item["value"]: item for item in response["payment_method_catalog"]["countries"]}
        assert response["total"] == 3
        assert countries["VN"]["count"] == 2
        assert {item["value"]: item["count"] for item in countries["VN"]["methods"]} == {
            "card": 1,
            "link": 2,
        }
        assert countries["PH"]["methods"] == [
            {"value": "gcash", "label": "GCash", "count": 1}
        ]


def test_probe_failed_keeps_confirmed_index_and_no_methods_clears_it(payment_engine):
    with Session(payment_engine) as session:
        account = _account(21, "VN", ["link"])
        session.add(account)
        session.commit()
        account_filters.refresh_account_list_state(session)
        assert _filtered_ids(session, [{"country": "VN", "methods": ["link"]}]) == [21]

        account = session.get(AccountModel, 21)
        assert account is not None
        extra = account.get_extra()
        extra["chatgpt_payment_methods"]["last_attempt"] = {
            "state": "probe_failed",
            "checked_at": "2026-08-29T01:00:00Z",
            "evidence": {"country": "US", "methods": ["paypal"]},
        }
        account.updated_at = datetime.now(timezone.utc)
        account.set_extra(extra)
        session.add(account)
        session.commit()
        account_filters.refresh_account_list_state(session, stale_only=True)
        assert _filtered_ids(session, [{"country": "VN", "methods": ["link"]}]) == [21]
        assert _filtered_ids(session, [{"country": "US", "methods": ["paypal"]}]) == []

        extra["chatgpt_payment_methods"] = {
            "confirmed_state": "no_methods",
            "confirmed_at": "2026-08-29T02:00:00Z",
            "evidence": {"country": "VN", "methods": []},
        }
        account.updated_at = datetime.now(timezone.utc)
        account.set_extra(extra)
        session.add(account)
        session.commit()
        account_filters.refresh_account_list_state(session, stale_only=True)
        assert session.exec(
            select(AccountPaymentMethodIndexModel).where(
                AccountPaymentMethodIndexModel.account_id == 21
            )
        ).all() == []


def test_legacy_gcash_marker_remains_filterable(payment_engine):
    with Session(payment_engine) as session:
        account = AccountModel(
            id=31,
            platform="chatgpt",
            email="legacy-gcash@example.com",
            password="",
            updated_at=datetime.now(timezone.utc),
        )
        account.set_extra(
            {
                "chatgpt_gcash_payment_method": {
                    "confirmed_state": "available",
                    "confirmed_at": "2026-08-29T00:00:00Z",
                    "evidence": {"country": "PH", "methods": ["gcash"]},
                }
            }
        )
        session.add(account)
        session.commit()
        account_filters.refresh_account_list_state(session)

        assert _filtered_ids(session, [{"country": "PH", "methods": ["gcash"]}]) == [31]
        old_query, _, _ = account_filters.account_filtered_query(
            session,
            platform="chatgpt",
            filter_source={"gcash_payment_method_state": "available"},
        )
        assert [int(row.id) for row in session.exec(old_query).all()] == [31]

        account = session.get(AccountModel, 31)
        assert account is not None
        extra = account.get_extra()
        extra["chatgpt_gcash_payment_method"]["confirmed_state"] = "unavailable"
        extra["chatgpt_gcash_payment_method"]["confirmed_at"] = "2026-08-29T01:00:00Z"
        account.updated_at = datetime.now(timezone.utc)
        account.set_extra(extra)
        session.add(account)
        session.commit()
        account_filters.refresh_account_list_state(session, stale_only=True)
        assert _filtered_ids(session, [{"country": "PH", "methods": ["gcash"]}]) == []
        assert session.exec(
            select(AccountPaymentMethodIndexModel).where(
                AccountPaymentMethodIndexModel.account_id == 31
            )
        ).all() == []
