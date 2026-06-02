import unittest

try:
    from core.db import AccountModel
    from services.account_filters import sort_account_rows

    HAS_DB_DEPS = True
except ModuleNotFoundError as exc:
    if exc.name != "sqlmodel":
        raise
    AccountModel = None
    sort_account_rows = None
    HAS_DB_DEPS = False


@unittest.skipUnless(HAS_DB_DEPS, "sqlmodel is not installed in this environment")
class AccountFilterSortTests(unittest.TestCase):
    def _account(self, account_id: int, expires_at="") -> AccountModel:
        account = AccountModel(
            id=account_id,
            platform="chatgpt",
            email=f"user{account_id}@example.com",
            password="",
        )
        if expires_at:
            account.set_extra(
                {
                    "chatgpt_local": {
                        "subscription": {
                            "subscription_active_until": expires_at,
                        },
                    },
                }
            )
        return account

    def test_sort_subscription_active_until_ascending_with_empty_last(self):
        rows = [
            self._account(1, ""),
            self._account(2, "1781089634000"),
            self._account(3, "2026-05-01T00:00:00+00:00"),
            self._account(4, "1781089634"),
        ]

        sorted_rows = sort_account_rows(rows, sort_by="subscription_active_until", sort_order="asc")

        self.assertEqual([row.id for row in sorted_rows], [3, 2, 4, 1])

    def test_sort_subscription_active_until_descending_with_empty_last(self):
        rows = [
            self._account(1, ""),
            self._account(2, "2026-05-01T00:00:00+00:00"),
            self._account(3, "1781089634"),
        ]

        sorted_rows = sort_account_rows(rows, sort_by="subscription_active_until", sort_order="descend")

        self.assertEqual([row.id for row in sorted_rows], [3, 2, 1])


if __name__ == "__main__":
    unittest.main()
