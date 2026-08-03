from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

import api.tasks as tasks_api
from api.tasks import BatchPaymentLinkTaskRequest
from core import db as core_db
from core.db import AccountModel, PaymentLinkGenerationModel


class TestPaymentLinkTaskGuard:
    def setup_method(self):
        self.engine = create_engine("sqlite://")
        self.core_engine_patch = patch.object(core_db, "engine", self.engine)
        self.tasks_engine_patch = patch.object(tasks_api, "engine", self.engine)
        self.core_engine_patch.start()
        self.tasks_engine_patch.start()
        SQLModel.metadata.create_all(self.engine)

    def teardown_method(self):
        self.tasks_engine_patch.stop()
        self.core_engine_patch.stop()

    def _add_account(
        self,
        *,
        email: str,
        cached: dict | None = None,
        legacy_cached: dict | None = None,
        status: str = "registered",
        web_session: bool = False,
    ) -> int:
        row = AccountModel(
            platform="chatgpt",
            email=email,
            password="pw",
            token="access-token",
            status=status,
        )
        extra = {}
        if isinstance(cached, dict):
            extra["chatgpt_last_payment_link"] = dict(cached)
        if isinstance(legacy_cached, dict):
            extra["chatgpt_paypal_url"] = dict(legacy_cached)
        if web_session:
            extra["cookies"] = "__Secure-next-auth.session-token=web-session"
        row.set_extra(extra)
        with Session(self.engine) as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return int(row.id or 0)

    def _add_history(
        self,
        account_id: int,
        *,
        status: str,
        url: str = "",
        updated_at: datetime | None = None,
        result: dict | None = None,
    ) -> None:
        with Session(self.engine) as session:
            account = session.get(AccountModel, account_id)
            assert account is not None
            row = PaymentLinkGenerationModel(
                account_id=account_id,
                account_email=str(account.email or "").strip().lower(),
                account_created_at=tasks_api._payment_link_account_created_at_text(account.created_at),
                task_id=f"task-{account_id}-{status}",
                request_id=f"request-{account_id}-{status}",
                status=status,
                url=url,
            )
            if updated_at is not None:
                row.updated_at = updated_at
            if isinstance(result, dict):
                row.set_result(result)
            session.add(row)
            session.commit()

    def _resolve(self, account_id: int, *, force_refresh: bool = False, params: dict | None = None):
        return tasks_api._resolve_batch_payment_link_accounts(
            BatchPaymentLinkTaskRequest(
                account_ids=[account_id],
                force_refresh=force_refresh,
                params=params or {},
            )
        )

    def test_short_link_requires_web_session_and_accepts_session_backed_account(self):
        short_params = {
            "plan": "plus",
            "payment_source": "chatgpt_hosted",
            "payment_link_format": "short_chatgpt",
            "country": "US",
            "currency": "USD",
        }
        missing_id = self._add_account(email="short-no-session@example.com")
        eligible, _, skipped, _ = self._resolve(missing_id, params=short_params)
        assert eligible == []
        assert "Web Session" in skipped[0]["reason"]

        session_id = self._add_account(email="short-session@example.com", web_session=True)
        eligible, _, skipped, _ = self._resolve(session_id, params=short_params)
        assert [item["account_id"] for item in eligible] == [session_id]
        assert skipped == []

    def test_short_and_long_success_guards_do_not_block_each_other(self):
        short_params = {
            "plan": "plus",
            "payment_source": "chatgpt_hosted",
            "payment_link_format": "short_chatgpt",
            "country": "US",
            "currency": "USD",
        }
        long_cache_id = self._add_account(
            email="long-to-short@example.com",
            web_session=True,
            cached={
                "url": "https://pay.openai.com/c/pay/cs_live_long#fid",
                "plan": "plus",
                "country": "US",
                "currency": "USD",
                "payment_source": "long_link",
                "payment_link_format": "long_link",
                "profile_hash": "profile-long",
            },
        )
        eligible, _, skipped, _ = self._resolve(long_cache_id, params=short_params)
        assert [item["account_id"] for item in eligible] == [long_cache_id]
        assert skipped == []

        short_history_id = self._add_account(email="short-to-long@example.com", web_session=True)
        self._add_history(
            short_history_id,
            status="succeeded",
            url="https://chatgpt.com/checkout/openai_llc/cs_live_short",
            result={
                **short_params,
                "url": "https://chatgpt.com/checkout/openai_llc/cs_live_short",
            },
        )
        eligible, _, skipped, _ = self._resolve(short_history_id)
        assert [item["account_id"] for item in eligible] == [short_history_id]
        assert skipped == []

    def test_success_history_and_cleaned_tombstone_block_normal_generation(self):
        history_account = self._add_account(email="history@example.com")
        self._add_history(
            history_account,
            status="succeeded",
            url="https://pay.example.test/history",
        )
        eligible, _, skipped, _ = self._resolve(history_account)
        assert eligible == []
        assert "提取成功" in skipped[0]["reason"]

        cleaned_account = self._add_account(
            email="cleaned@example.com",
            cached={"link_status": "expired_cleaned"},
        )
        eligible, _, skipped, _ = self._resolve(cleaned_account)
        assert eligible == []
        assert "已清理" in skipped[0]["reason"]

        # Explicit force refresh is the operator escape hatch for an expired
        # or cancelled link, but not for a paid terminal.
        eligible, _, skipped, _ = self._resolve(cleaned_account, force_refresh=True)
        assert [item["account_id"] for item in eligible] == [cleaned_account]
        assert skipped == []

        paid_cleaned_account = self._add_account(
            email="paid-cleaned@example.com",
            cached={"link_status": "paid_cleaned"},
        )
        eligible, _, skipped, _ = self._resolve(paid_cleaned_account, force_refresh=True)
        assert eligible == []
        assert "支付完成" in skipped[0]["reason"]

    def test_current_valid_url_is_success_evidence_but_malformed_url_is_not(self):
        current_id = self._add_account(
            email="current@example.com",
            cached={
                "url": "https://pay.example.test/current",
                "payment_link_format": "long_link",
            },
        )
        eligible, _, skipped, _ = self._resolve(current_id)
        assert eligible == []
        assert "当前支付链接" in skipped[0]["reason"]

        legacy_id = self._add_account(
            email="legacy-paypal@example.com",
            legacy_cached={
                "approval_url": "https://www.paypal.com/agreements/approve?ba_token=BA-LEGACY",
            },
        )
        eligible, _, skipped, _ = self._resolve(legacy_id)
        assert eligible == []
        assert "当前支付链接" in skipped[0]["reason"]
        eligible, _, skipped, _ = self._resolve(legacy_id, force_refresh=True)
        assert [item["account_id"] for item in eligible] == [legacy_id]
        assert skipped == []

        malformed_id = self._add_account(
            email="malformed@example.com",
            cached={
                "url": "not-a-url",
                "payment_link_format": "long_link",
            },
        )
        self._add_history(
            malformed_id,
            status="succeeded",
            url="not-a-url",
        )
        eligible, _, skipped, _ = self._resolve(malformed_id)
        assert [item["account_id"] for item in eligible] == [malformed_id]
        assert skipped == []

    def test_force_refresh_can_bypass_success_history_but_not_paid_or_invalid(self):
        account_id = self._add_account(email="force-history@example.com")
        self._add_history(account_id, status="succeeded", url="https://pay.example.test/old")
        eligible, _, skipped, _ = self._resolve(account_id, force_refresh=True)
        assert [item["account_id"] for item in eligible] == [account_id]
        assert skipped == []

        invalid_id = self._add_account(email="invalid@example.com", status="invalid")
        eligible, _, skipped, _ = self._resolve(invalid_id, force_refresh=True)
        assert eligible == []
        assert "不能生成" in skipped[0]["reason"]

        subscribed_id = self._add_account(email="subscribed@example.com", status="subscribed")
        eligible, _, skipped, _ = self._resolve(subscribed_id, force_refresh=True)
        assert eligible == []
        assert "不能生成" in skipped[0]["reason"]

        generated_status_id = self._add_account(
            email="generation-status-is-not-paid@example.com",
            cached={"link_status": "succeeded"},
        )
        eligible, _, skipped, _ = self._resolve(generated_status_id, force_refresh=True)
        assert [item["account_id"] for item in eligible] == [generated_status_id]
        assert skipped == []

    def test_fresh_inflight_history_blocks_and_stale_history_releases(self):
        fresh_id = self._add_account(email="fresh-running@example.com")
        self._add_history(
            fresh_id,
            status="running",
            updated_at=datetime.now(timezone.utc),
        )
        for force_refresh in (False, True):
            eligible, _, skipped, _ = self._resolve(fresh_id, force_refresh=force_refresh)
            assert eligible == []
            assert "正在进行" in skipped[0]["reason"]

        stale_id = self._add_account(email="stale-running@example.com")
        stale_at = datetime.now(timezone.utc) - timedelta(
            seconds=tasks_api._payment_link_inflight_ttl_seconds() + 60
        )
        self._add_history(stale_id, status="running", updated_at=stale_at)
        eligible, _, skipped, _ = self._resolve(stale_id)
        assert [item["account_id"] for item in eligible] == [stale_id]
        assert skipped == []

    def test_non_succeeded_history_can_be_retried(self):
        for index, status in enumerate(("failed", "interrupted", "success", "completed", "done"), start=1):
            account_id = self._add_account(email=f"retry-{index}@example.com")
            self._add_history(
                account_id,
                status=status,
                url=f"https://pay.example.test/non-succeeded-{index}",
            )
            eligible, _, skipped, _ = self._resolve(account_id)
            assert [item["account_id"] for item in eligible] == [account_id]
            assert skipped == []

    def test_reused_account_id_ignores_mismatched_legacy_history(self):
        original_id = self._add_account(email="legacy-original@example.com")
        self._add_history(
            original_id,
            status="succeeded",
            url="https://pay.example.test/legacy-original",
        )
        with Session(self.engine) as session:
            original = session.get(AccountModel, original_id)
            assert original is not None
            session.delete(original)
            session.commit()
            replacement = AccountModel(
                id=original_id,
                platform="chatgpt",
                email="legacy-replacement@example.com",
                password="pw",
                token="replacement-token",
            )
            replacement.set_extra({})
            session.add(replacement)
            session.commit()

        eligible, _, skipped, _ = self._resolve(original_id)
        assert [item["account_id"] for item in eligible] == [original_id]
        assert skipped == []
        history = tasks_api.list_chatgpt_payment_link_history(account_id=original_id)
        assert history["total"] == 0
