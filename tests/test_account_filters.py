import unittest
from datetime import datetime, timezone
from unittest import mock

try:
    from sqlalchemy import text
    from sqlmodel import Session, select

    from core.db import AccountListStateModel, AccountModel, PaymentLinkGenerationModel, engine, init_db
    from services.account_filters import (
        account_has_submitted,
        account_idea_submit_state,
        account_payment_link_platform,
        account_payment_link_generated,
        account_payment_link_summary,
        account_phone_binding_state,
        account_oaipay_upload_state,
        account_revival_info,
        account_revival_state,
        account_sub2api_upload_state,
        account_submission_info,
        account_submit_state,
        apply_account_list_state_filters,
        apply_account_list_state_sort,
        delete_account_list_state_for_account_ids,
        filter_account_rows,
        normalize_account_sort_specs,
        refresh_account_list_state,
        refresh_stale_account_list_state,
        sort_account_rows,
        upsert_account_list_state_for_account_ids,
    )

    HAS_DB_DEPS = True
except ModuleNotFoundError as exc:
    if exc.name != "sqlmodel":
        raise
    AccountListStateModel = None
    AccountModel = None
    PaymentLinkGenerationModel = None
    Session = None
    engine = None
    init_db = None
    account_idea_submit_state = None
    account_has_submitted = None
    account_payment_link_platform = None
    account_payment_link_generated = None
    account_payment_link_summary = None
    account_phone_binding_state = None
    account_oaipay_upload_state = None
    account_revival_info = None
    account_revival_state = None
    account_sub2api_upload_state = None
    account_submission_info = None
    account_submit_state = None
    apply_account_list_state_filters = None
    apply_account_list_state_sort = None
    delete_account_list_state_for_account_ids = None
    filter_account_rows = None
    normalize_account_sort_specs = None
    refresh_account_list_state = None
    refresh_stale_account_list_state = None
    select = None
    sort_account_rows = None
    text = None
    upsert_account_list_state_for_account_ids = None
    HAS_DB_DEPS = False


@unittest.skipUnless(HAS_DB_DEPS, "sqlmodel is not installed in this environment")
class AccountFilterSortTests(unittest.TestCase):
    def _account(self, account_id: int, expires_at="", *, created_at: datetime | None = None) -> AccountModel:
        account = AccountModel(
            id=account_id,
            platform="chatgpt",
            email=f"user{account_id}@example.com",
            password="",
        )
        if created_at is not None:
            account.created_at = created_at
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

    def test_sort_specs_default_and_legacy_expiry_compatibility(self):
        self.assertEqual(normalize_account_sort_specs(), (("created_at", "asc"),))
        self.assertEqual(
            normalize_account_sort_specs("subscription_active_until", "descend"),
            (("subscription_active_until", "desc"), ("created_at", "asc")),
        )
        self.assertEqual(
            normalize_account_sort_specs(
                "subscription_active_until,created_at",
                "asc,desc",
            ),
            (("subscription_active_until", "asc"), ("created_at", "desc")),
        )
        self.assertEqual(
            normalize_account_sort_specs("unsupported", "desc"),
            (("created_at", "asc"),),
        )

    def test_sort_registration_time_defaults_oldest_first_and_supports_descending(self):
        rows = [
            self._account(1, created_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
            self._account(2, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            self._account(3, created_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
        ]

        self.assertEqual([row.id for row in sort_account_rows(rows)], [2, 1, 3])
        self.assertEqual(
            [row.id for row in sort_account_rows(rows, sort_by="created_at", sort_order="desc")],
            [3, 1, 2],
        )

    def test_sort_expiry_then_registration_time(self):
        rows = [
            self._account(1, "2030-01-02T00:00:00Z", created_at=datetime(2026, 1, 3, tzinfo=timezone.utc)),
            self._account(2, "2030-01-01T00:00:00Z", created_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
            self._account(3, "2030-01-01T00:00:00Z", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            self._account(4, "", created_at=datetime(2026, 1, 4, tzinfo=timezone.utc)),
        ]

        self.assertEqual(
            [
                row.id
                for row in sort_account_rows(
                    rows,
                    sort_by="subscription_active_until,created_at",
                    sort_order="asc,asc",
                )
            ],
            [3, 2, 1, 4],
        )
        self.assertEqual(
            [
                row.id
                for row in sort_account_rows(
                    rows,
                    sort_by="subscription_active_until,created_at",
                    sort_order="asc,desc",
                )
            ],
            [2, 3, 1, 4],
        )

    def _payment_history(self, account: AccountModel, *, request_id: str, status: str, url: str = "") -> PaymentLinkGenerationModel:
        return PaymentLinkGenerationModel(
            account_id=int(account.id or 0),
            account_email=str(account.email or "").strip().lower(),
            account_created_at=account.created_at.replace(tzinfo=None).isoformat(sep=" "),
            request_id=request_id,
            status=status,
            url=url,
        )

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

    def test_revival_info_infers_legacy_invalid_recheck_success(self):
        account = self._account(10)
        account.set_extra(
            {
                "chatgpt_invalid_recheck": {
                    "status": "recovered_access_token",
                    "source": "invalid_account_recheck",
                    "task_id": "task-recheck",
                    "checked_at": "2026-06-12T04:00:00+00:00",
                    "has_access_token": True,
                },
            }
        )

        info = account_revival_info(account)

        self.assertEqual(info["state"], "revived")
        self.assertEqual(info["kind"], "invalid_recheck")
        self.assertEqual(info["label"], "失效测活恢复")
        self.assertEqual(info["at"], "2026-06-12T04:00:00+00:00")
        self.assertTrue(info["legacy_inferred"])

    def test_revival_info_uses_explicit_marker_when_present(self):
        account = self._account(11)
        account.set_extra(
            {
                "chatgpt_last_revival": {
                    "source": "custom_email_recheck",
                    "mode": "create_new",
                    "task_id": "task-custom",
                    "revived_at": "2026-06-12T05:00:00+00:00",
                    "auth_level": "access_token_only",
                },
            }
        )

        info = account_revival_info(account)

        self.assertEqual(info["state"], "recovery_new")
        self.assertEqual(info["kind"], "custom_email_recheck_new")
        self.assertEqual(info["label"], "邮箱测活新建")
        self.assertFalse(info["legacy_inferred"])

    def test_filter_account_rows_supports_revival_state(self):
        legacy_revived = self._account(20)
        legacy_revived.set_extra(
            {
                "chatgpt_invalid_recheck": {
                    "status": "recovered_access_token",
                    "source": "invalid_account_recheck",
                    "task_id": "task-recheck",
                    "checked_at": "2026-06-12T04:00:00+00:00",
                    "has_access_token": True,
                },
            }
        )
        recovery_new = self._account(21)
        recovery_new.set_extra(
            {
                "chatgpt_last_revival": {
                    "source": "custom_email_recheck",
                    "mode": "create_new",
                    "revived_at": "2026-06-12T05:00:00+00:00",
                },
            }
        )
        normal = self._account(22)

        self.assertEqual(account_revival_state(legacy_revived), "revived")
        self.assertEqual(account_revival_state(recovery_new), "recovery_new")
        self.assertEqual(account_revival_state(normal), "none")

        revived_rows = filter_account_rows([legacy_revived, recovery_new, normal], revival_state="revived")
        self.assertEqual([row.id for row in revived_rows], [20])

        recovery_new_rows = filter_account_rows([legacy_revived, recovery_new, normal], revival_state="recovery_new")
        self.assertEqual([row.id for row in recovery_new_rows], [21])

    def test_filter_account_rows_supports_idea_submit_state(self):
        unavailable = self._account(30)
        unavailable.set_extra(
            {
                "idea_submit": {
                    "unavailable": True,
                    "reason": "该账号没有开通资格",
                },
                "idea_submit_unavailable": True,
            }
        )
        paid = self._account(31)
        paid.set_extra({"baxigpt_cdk": {"status": "paid"}})
        legacy_unavailable = self._account(32)
        legacy_unavailable.set_extra(
            {
                "chatgpt_account_unavailable": True,
                "chatgpt_unavailable_reason": "legacy",
                "baxigpt_cdk": {"status": "failed"},
            }
        )
        available = self._account(33)
        submitted = self._account(34)
        submitted.set_extra({"baxigpt_cdk": {"status": "submitted"}})
        processing = self._account(35)
        processing.set_extra({"baxigpt_cdk": {"status": "processing"}})

        self.assertEqual(account_idea_submit_state(unavailable), "unavailable")
        self.assertEqual(account_idea_submit_state(paid), "paid")
        self.assertEqual(account_idea_submit_state(legacy_unavailable), "unavailable")
        self.assertEqual(account_idea_submit_state(available), "available")

        self.assertEqual(
            [row.id for row in filter_account_rows([unavailable, paid, legacy_unavailable, available, submitted, processing], idea_submit_state="unavailable")],
            [30, 32],
        )
        self.assertEqual(
            [row.id for row in filter_account_rows([unavailable, paid, legacy_unavailable, available, submitted, processing], idea_submit_state="paid,unsubmitted")],
            [31, 33],
        )
        self.assertEqual(
            [row.id for row in filter_account_rows([unavailable, paid, legacy_unavailable, available, submitted, processing], idea_submit_state="submitting")],
            [34, 35],
        )
        self.assertEqual(
            [row.id for row in filter_account_rows([unavailable, paid, legacy_unavailable, available, submitted, processing], idea_submit_state="available,submitted")],
            [33, 34],
        )

    def test_generic_submission_state_keeps_outcome_and_submission_evidence_independent(self):
        pix_failed = self._account(36)
        pix_failed.set_extra(
            {
                "idea_submit": {"status": "failed", "unavailable": True, "order_id": "pix-order"},
                "idea_submit_unavailable": True,
                "baxigpt_cdk": {"status": "failed", "order_id": "pix-order"},
                "chatgpt_last_payment_link": {"link_status": "pix_submitted"},
            }
        )
        pix_link_only = self._account(37)
        pix_link_only.set_extra(
            {"chatgpt_last_payment_link": {"link_status": "pix_submitted"}}
        )
        timeout = self._account(38)
        timeout.set_extra(
            {"baxigpt_cdk": {"status": "timeout", "display_id": "timeout-order"}}
        )
        unavailable_only = self._account(39)
        unavailable_only.set_extra(
            {"idea_submit": {"unavailable": True}, "idea_submit_unavailable": True}
        )
        failed_before_submit = self._account(40)
        failed_before_submit.set_extra({"baxigpt_cdk": {"status": "failed"}})
        available = self._account(41)
        rows = [pix_failed, pix_link_only, timeout, unavailable_only, failed_before_submit, available]

        self.assertEqual(account_idea_submit_state(pix_failed), "unavailable")
        self.assertEqual(account_submit_state(pix_failed), "failed")
        self.assertTrue(account_has_submitted(pix_failed))
        self.assertEqual(
            account_submission_info(pix_failed),
            {
                "state": "failed",
                "has_submitted": True,
                "link_submitted": True,
                "link_status": "pix_submitted",
                "unavailable": True,
            },
        )
        self.assertEqual(account_submit_state(pix_link_only), "submitted")
        self.assertTrue(account_has_submitted(pix_link_only))
        self.assertEqual(account_submit_state(timeout), "timeout")
        self.assertTrue(account_has_submitted(timeout))
        self.assertEqual(account_submit_state(unavailable_only), "unavailable")
        self.assertFalse(account_has_submitted(unavailable_only))
        self.assertEqual(account_submit_state(failed_before_submit), "failed")
        self.assertFalse(account_has_submitted(failed_before_submit))

        self.assertEqual(
            [row.id for row in filter_account_rows(rows, submit_state="submitting")],
            [37],
        )
        self.assertEqual(
            [row.id for row in filter_account_rows(rows, has_submitted=True)],
            [36, 37, 38],
        )
        self.assertEqual(
            [row.id for row in filter_account_rows(rows, has_submitted=False)],
            [39, 40, 41],
        )

    def test_phone_binding_filter_uses_confirmed_binding_not_rt_or_phone_hint(self):
        init_db()
        confirmed = self._account(501)
        confirmed.set_extra(
            {
                "refresh_token": "rt-confirmed",
                "chatgpt_phone_binding": {
                    "status": "bound",
                    "phone": "+1 (613) 465-5704",
                },
            }
        )
        observed_only = self._account(502)
        observed_only.set_extra(
            {
                "refresh_token": "rt-observed-only",
                "chatgpt_bound_phone": {
                    "phone": "+16134655704",
                    "verification_status": "required",
                },
            }
        )
        malformed = self._account(503)
        malformed.set_extra(
            {
                "chatgpt_phone_binding": {
                    "status": "success",
                    "phone": "5704",
                },
            }
        )
        no_phone_record = self._account(504)
        no_phone_record.set_extra({"refresh_token": "rt-without-phone"})
        rows = [confirmed, observed_only, malformed, no_phone_record]

        self.assertEqual(account_phone_binding_state(confirmed), "confirmed")
        self.assertEqual(account_phone_binding_state(observed_only), "unconfirmed")
        self.assertEqual(account_phone_binding_state(malformed), "unconfirmed")
        self.assertEqual(account_phone_binding_state(no_phone_record), "unknown")
        self.assertEqual(
            [row.id for row in filter_account_rows(rows, phone_binding_state="confirmed")],
            [501],
        )
        self.assertEqual(
            [row.id for row in filter_account_rows(rows, phone_binding_state="unbound")],
            [502, 503, 504],
        )

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state"))
            session.exec(text("DELETE FROM accounts"))
            for row in rows:
                session.add(row)
            session.commit()
            refresh_account_list_state(session)

            state_rows = {
                int(row[0]): str(row[1])
                for row in session.exec(
                    text(
                        """
                        SELECT account_id, phone_binding_state
                        FROM account_list_state
                        WHERE account_id BETWEEN 501 AND 504
                        ORDER BY account_id
                        """
                    )
                ).all()
            }
            self.assertEqual(
                state_rows,
                {
                    501: "confirmed",
                    502: "unconfirmed",
                    503: "unconfirmed",
                    504: "unknown",
                },
            )

            def sql_ids(**filters):
                q = select(AccountModel).join(
                    AccountListStateModel,
                    AccountListStateModel.account_id == AccountModel.id,
                )
                q = apply_account_list_state_filters(q, **filters)
                q = q.order_by(AccountModel.id.asc())
                return [int(row.id or 0) for row in session.exec(q).all()]

            self.assertEqual(sql_ids(phone_binding_state="confirmed"), [501])
            self.assertEqual(sql_ids(phone_binding_state="unbound"), [502, 503, 504])

    def test_payment_link_platform_filter_keeps_pix_paypal_and_no_link_distinct(self):
        init_db()
        pix = self._account(511)
        pix.set_extra(
            {
                "chatgpt_last_payment_link": {
                    "url": "https://payments.stripe.com/qr/instructions/pix-511",
                    "link_type": "pix",
                    "payment_link_format": "long_link",
                },
            }
        )
        legacy_paypal = self._account(512)
        legacy_paypal.set_extra(
            {
                "chatgpt_paypal_url": {
                    "approval_url": "https://www.paypal.com/agreements/approve?ba_token=BA-512",
                },
            }
        )
        hosted = self._account(513)
        hosted.set_extra(
            {
                "chatgpt_last_payment_link": {
                    "url": "https://chatgpt.com/checkout/openai_llc/cs_513",
                    "link_type": "hosted",
                    "payment_link_format": "long_hosted",
                },
            }
        )
        other = self._account(514)
        other.set_extra(
            {
                "chatgpt_last_payment_link": {
                    "url": "https://pay.example.test/checkout/514",
                    "link_type": "ideal",
                    "payment_link_format": "ideal_url",
                },
            }
        )
        no_link = self._account(515)
        malformed = self._account(516)
        malformed.set_extra(
            {
                "chatgpt_last_payment_link": {
                    "url": "javascript:alert('not-a-payment-link')",
                    "link_type": "pix",
                },
            }
        )
        malformed_newer_with_legacy_paypal = self._account(517)
        malformed_newer_with_legacy_paypal.set_extra(
            {
                "chatgpt_last_payment_link": {
                    "url": "javascript:alert('bad-newer-cache')",
                    "link_type": "pix",
                },
                "chatgpt_paypal_url": {
                    "approval_url": "https://www.paypal.com/agreements/approve?ba_token=BA-517",
                },
            }
        )
        rows = [pix, legacy_paypal, hosted, other, no_link, malformed, malformed_newer_with_legacy_paypal]

        self.assertEqual(account_payment_link_platform(pix), "pix")
        self.assertEqual(account_payment_link_platform(legacy_paypal), "paypal")
        self.assertEqual(account_payment_link_platform(hosted), "chatgpt")
        self.assertEqual(account_payment_link_platform(other), "other")
        self.assertEqual(account_payment_link_platform(no_link), "none")
        self.assertEqual(account_payment_link_platform(malformed), "none")
        self.assertEqual(account_payment_link_platform(malformed_newer_with_legacy_paypal), "paypal")
        self.assertEqual(
            [row.id for row in filter_account_rows(rows, payment_link_platform="pix,paypal")],
            [511, 512, 517],
        )
        self.assertEqual(
            [row.id for row in filter_account_rows(rows, payment_link_platform="no_payment_link")],
            [515, 516],
        )

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state"))
            session.exec(text("DELETE FROM accounts"))
            for row in rows:
                session.add(row)
            session.commit()
            refresh_account_list_state(session)

            state_rows = {
                int(row[0]): str(row[1])
                for row in session.exec(
                    text(
                        """
                        SELECT account_id, payment_link_platform
                        FROM account_list_state
                        WHERE account_id BETWEEN 511 AND 517
                        ORDER BY account_id
                        """
                    )
                ).all()
            }
            self.assertEqual(
                state_rows,
                {
                    511: "pix",
                    512: "paypal",
                    513: "chatgpt",
                    514: "other",
                    515: "none",
                    516: "none",
                    517: "paypal",
                },
            )

            def sql_ids(**filters):
                q = select(AccountModel).join(
                    AccountListStateModel,
                    AccountListStateModel.account_id == AccountModel.id,
                )
                q = apply_account_list_state_filters(q, **filters)
                q = q.order_by(AccountModel.id.asc())
                return [int(row.id or 0) for row in session.exec(q).all()]

            self.assertEqual(sql_ids(payment_link_platform="pix,paypal"), [511, 512, 517])
            self.assertEqual(sql_ids(payment_link_platform="none"), [515, 516])

    def test_payment_link_platform_auto_classifies_upi_from_payment_method_or_url(self):
        upi_by_method = self._account(518)
        upi_by_method.set_extra(
            {
                "chatgpt_last_payment_link": {
                    "url": "https://payments.stripe.com/upi/instructions/method",
                    "link_type": "hosted",
                    "payment_method_type": "upi",
                }
            }
        )
        upi_by_url = self._account(519)
        upi_by_url.set_extra(
            {
                "chatgpt_last_payment_link": {
                    "url": "https://payments.stripe.com/upi/instructions/url",
                }
            }
        )
        self.assertEqual(account_payment_link_platform(upi_by_method), "upi")
        self.assertEqual(account_payment_link_platform(upi_by_url), "upi")
        self.assertEqual(
            [row.id for row in filter_account_rows([upi_by_method, upi_by_url], payment_link_platform="upi")],
            [518, 519],
        )

    def test_payment_link_generated_filter_keeps_history_after_url_cleanup(self):
        init_db()
        cleaned = self._account(531)
        cleaned.set_extra(
            {
                "chatgpt_last_payment_link": {
                    "link_type": "pix",
                    "link_status": "expired_cleaned",
                    "generated_at": "2026-07-01T00:00:00Z",
                }
            }
        )
        current = self._account(532)
        current.set_extra(
            {
                "chatgpt_last_payment_link": {
                    "url": "https://payments.example.test/current-532",
                    "link_type": "pix",
                }
            }
        )
        history_only = self._account(533)
        failed_only = self._account(534)
        invalid_history = self._account(535)

        with Session(engine) as session:
            ids = (531, 532, 533, 534, 535)
            placeholders = ",".join(str(value) for value in ids)
            session.exec(text(f"DELETE FROM payment_link_generations WHERE account_id IN ({placeholders})"))
            session.exec(text(f"DELETE FROM account_list_state WHERE account_id IN ({placeholders})"))
            session.exec(text(f"DELETE FROM accounts WHERE id IN ({placeholders})"))
            session.add_all([cleaned, current, history_only, failed_only, invalid_history])
            session.add(
                self._payment_history(
                    history_only,
                    request_id="account-filter-history-533",
                    status="succeeded",
                    url="https://payments.example.test/history-533",
                )
            )
            session.add(
                self._payment_history(
                    failed_only,
                    request_id="account-filter-failed-534",
                    status="failed",
                    url="https://payments.example.test/failed-534",
                )
            )
            session.add(
                self._payment_history(
                    invalid_history,
                    request_id="account-filter-invalid-535",
                    status="succeeded",
                    url="javascript:alert('not-a-link')",
                )
            )
            session.commit()
            refresh_account_list_state(session)

            cached = {
                int(row[0]): bool(row[1])
                for row in session.exec(
                    text(
                        """
                        SELECT account_id, payment_link_generated
                        FROM account_list_state
                        WHERE account_id BETWEEN 531 AND 535
                        ORDER BY account_id
                        """
                    )
                ).all()
            }
            self.assertEqual(cached, {531: True, 532: True, 533: True, 534: False, 535: False})

            def sql_ids(generated):
                query = select(AccountModel).join(
                    AccountListStateModel,
                    AccountListStateModel.account_id == AccountModel.id,
                ).where(AccountModel.id.in_(ids))
                query = apply_account_list_state_filters(query, payment_link_generated=generated)
                return [int(row.id or 0) for row in session.exec(query.order_by(AccountModel.id.asc())).all()]

            self.assertEqual(sql_ids(True), [531, 532, 533])
            self.assertEqual(sql_ids(False), [534, 535])

        self.assertTrue(account_payment_link_generated(cleaned))
        self.assertTrue(account_payment_link_generated(current))

    def test_cleaned_payment_link_summary_keeps_metadata_without_url(self):
        account = self._account(536)
        account.set_extra(
            {
                "chatgpt_last_payment_link": {
                    "link_type": "pix",
                    "link_status": "paid_cleaned",
                    "generated_at": "2026-07-02T00:00:00Z",
                    "cleaned_at": "2026-07-03T00:00:00Z",
                }
            }
        )

        summary = account_payment_link_summary(account)

        self.assertEqual(summary["platform"], "none")
        self.assertEqual(summary["link_type"], "pix")
        self.assertEqual(summary["link_status"], "paid_cleaned")
        self.assertEqual(summary["generated_at"], "2026-07-02T00:00:00Z")
        self.assertNotIn("url", summary)

    def test_payment_link_sql_uses_valid_secondary_url_when_primary_marker_is_invalid(self):
        init_db()
        account = self._account(537)
        account.set_extra(
            {
                "chatgpt_last_payment_link": {
                    "url": "javascript:alert('bad-primary')",
                    "paypal_url": "https://www.paypal.com/agreements/approve?ba_token=BA-537",
                    "link_type": "pix",
                }
            }
        )

        self.assertEqual(account_payment_link_platform(account), "pix")

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state WHERE account_id = 537"))
            session.exec(text("DELETE FROM accounts WHERE id = 537"))
            session.add(account)
            session.commit()
            refresh_account_list_state(session)
            state = session.get(AccountListStateModel, 537)

        self.assertIsNotNone(state)
        self.assertEqual(state.payment_link_platform, "pix")
        self.assertTrue(state.payment_link_generated)

    def test_cleaned_tombstone_never_exposes_residual_url_or_resurrects_legacy_paypal(self):
        init_db()
        account = self._account(538)
        account.set_extra(
            {
                "chatgpt_last_payment_link": {
                    "url": "https://payments.example.test/expired-538",
                    "link_type": "pix",
                    "link_status": "expired_cleaned",
                    "generated_at": "2026-07-04T00:00:00Z",
                }
            }
        )

        self.assertEqual(account_payment_link_platform(account), "none")
        self.assertNotIn("url", account_payment_link_summary(account))

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state WHERE account_id = 538"))
            session.exec(text("DELETE FROM accounts WHERE id = 538"))
            session.add(account)
            session.commit()
            refresh_account_list_state(session)
            state = session.get(AccountListStateModel, 538)
            self.assertIsNotNone(state)
            self.assertEqual(state.payment_link_platform, "none")
            self.assertTrue(state.payment_link_generated)

            extra = account.get_extra()
            extra["chatgpt_paypal_url"] = {
                "approval_url": "https://www.paypal.com/agreements/approve?ba_token=BA-538"
            }
            account.set_extra(extra)
            session.add(account)
            session.commit()
            refresh_account_list_state(session, account_ids=[538])
            state = session.get(AccountListStateModel, 538)

        self.assertEqual(account_payment_link_platform(account), "none")
        self.assertEqual(state.payment_link_platform, "none")
        self.assertTrue(state.payment_link_generated)

    def test_invalid_legacy_paypal_url_is_not_promoted_by_sql_platform_filter(self):
        init_db()
        account = self._account(539)
        account.set_extra(
            {
                "chatgpt_last_payment_link": {"url": "javascript:alert('bad-primary')"},
                "chatgpt_paypal_url": {"approval_url": "https:///missing-host"},
            }
        )
        self.assertEqual(account_payment_link_platform(account), "none")

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state WHERE account_id = 539"))
            session.exec(text("DELETE FROM accounts WHERE id = 539"))
            session.add(account)
            session.commit()
            refresh_account_list_state(session)
            state = session.get(AccountListStateModel, 539)

        self.assertIsNotNone(state)
        self.assertEqual(state.payment_link_platform, "none")
        self.assertFalse(state.payment_link_generated)

    def test_overlong_payment_url_is_not_counted_as_current_or_generated(self):
        init_db()
        account = self._account(540)
        account.set_extra(
            {
                "chatgpt_last_payment_link": {
                    "url": "https://payments.example.test/" + ("x" * 9000),
                    "link_type": "pix",
                }
            }
        )
        self.assertEqual(account_payment_link_platform(account), "none")
        self.assertFalse(account_payment_link_generated(account))

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state WHERE account_id = 540"))
            session.exec(text("DELETE FROM accounts WHERE id = 540"))
            session.add(account)
            session.commit()
            refresh_account_list_state(session)
            state = session.get(AccountListStateModel, 540)

        self.assertIsNotNone(state)
        self.assertEqual(state.payment_link_platform, "none")
        self.assertFalse(state.payment_link_generated)

    def test_account_delete_cleans_payment_history_before_id_reuse(self):
        init_db()
        account_id = 541
        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state WHERE account_id = 541"))
            session.exec(text("DELETE FROM payment_link_generations WHERE account_id = 541"))
            session.exec(text("DELETE FROM accounts WHERE id = 541"))
            account = self._account(account_id)
            session.add(account)
            session.add(
                self._payment_history(
                    account,
                    request_id="account-delete-history-541",
                    status="succeeded",
                    url="https://payments.example.test/history-541",
                )
            )
            session.commit()

            session.exec(text("DELETE FROM accounts WHERE id = 541"))
            session.commit()
            remaining = session.exec(
                text("SELECT COUNT(*) FROM payment_link_generations WHERE account_id = 541")
            ).one()
            self.assertEqual(int(remaining[0]), 0)

            replacement = self._account(account_id)
            session.add(replacement)
            session.commit()
            refresh_account_list_state(session, account_ids=[account_id])
            state = session.get(AccountListStateModel, account_id)

        self.assertIsNotNone(state)
        self.assertFalse(state.payment_link_generated)

    def test_integration_upload_filter_uses_positive_evidence_and_migrates_legacy_states(self):
        uploaded_marker = self._account(520)
        uploaded_marker.set_extra({"sync_statuses": {"sub2api": {"uploaded": True, "remote_state": "unreachable"}}})
        remote_exists = self._account(521)
        remote_exists.set_extra({"sync_statuses": {"sub2api": {"remote_state": "exists"}}})
        successful_history = self._account(522)
        successful_history.set_extra({"sync_statuses": {"oaipay": {"remote_state": "not_found", "last_upload": {"status": "success"}}}})
        not_uploaded = self._account(523)
        not_uploaded.set_extra({"sync_statuses": {"sub2api": {"remote_state": "ambiguous"}}})
        unknown = self._account(524)
        rows = [uploaded_marker, remote_exists, successful_history, not_uploaded, unknown]

        self.assertEqual(account_sub2api_upload_state(uploaded_marker), "uploaded")
        self.assertEqual(account_sub2api_upload_state(remote_exists), "uploaded")
        self.assertEqual(account_oaipay_upload_state(successful_history), "uploaded")
        self.assertEqual(account_sub2api_upload_state(not_uploaded), "not_uploaded")
        self.assertEqual(account_sub2api_upload_state(unknown), "not_uploaded")
        self.assertEqual(
            [row.id for row in filter_account_rows(rows, sub2api_state="exists")],
            [520, 521],
        )
        self.assertEqual(
            [row.id for row in filter_account_rows(rows, sub2api_state="unknown,not_found,ambiguous")],
            [522, 523, 524],
        )
        self.assertEqual(
            [row.id for row in filter_account_rows(rows, sub2api_state="uploaded,not_uploaded")],
            [520, 521, 522, 523, 524],
        )

        init_db()
        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state"))
            session.exec(text("DELETE FROM accounts"))
            for row in rows:
                session.add(row)
            session.commit()
            refresh_account_list_state(session)
            states = {
                int(row.account_id): (row.sub2api_state, row.oaipay_state)
                for row in session.exec(select(AccountListStateModel)).all()
            }

        self.assertEqual(states[520][0], "uploaded")
        self.assertEqual(states[521][0], "uploaded")
        self.assertEqual(states[522][1], "uploaded")
        self.assertEqual(states[523][0], "not_uploaded")
        self.assertEqual(states[524], ("not_uploaded", "not_uploaded"))

    def test_account_list_state_sql_filters_match_python_filters(self):
        init_db()
        rows = [
            self._account(101, "2026-05-01T00:00:00+00:00"),
            self._account(102, "1781089634000"),
            self._account(103, ""),
        ]
        rows[0].token = ""
        rows[0].set_extra(
            {
                "refresh_token": "rt-101",
                "chatgpt_capabilities": {"subscription_plan": "plus"},
                "sync_statuses": {"sub2api": {"remote_state": "exists"}},
                "idea_submit": {"unavailable": True},
                "idea_submit_unavailable": True,
            }
        )
        rows[1].token = "at-102"
        rows[1].set_extra(
            {
                "chatgpt_local": {
                    "auth": {"state": "unauthorized"},
                    "subscription": {"plan": "free", "subscription_active_until": "1781089634000"},
                },
                "sync_statuses": {"sub2api": {"remote_state": "not_found"}},
                "chatgpt_last_revival": {"source": "custom_email_recheck", "mode": "create_new"},
                "baxigpt_cdk": {"status": "paid"},
            }
        )
        rows[2].set_extra(
            {
                "manually_used": True,
                "chatgpt_capabilities": {"subscription_plan": "free", "subscription_checked": True},
                "chatgpt_invalid_recheck": {
                    "status": "recovered_access_token",
                    "source": "invalid_account_recheck",
                    "task_id": "task-recheck",
                    "has_access_token": True,
                },
            }
        )

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state"))
            session.exec(text("DELETE FROM accounts"))
            for row in rows:
                session.add(row)
            session.commit()
            refresh_account_list_state(session)

            def sql_ids(**filters):
                q = select(AccountModel).join(
                    AccountListStateModel,
                    AccountListStateModel.account_id == AccountModel.id,
                )
                q = apply_account_list_state_filters(q, **filters)
                q = q.order_by(AccountModel.id.asc())
                return [int(row.id or 0) for row in session.exec(q).all()]

            self.assertEqual(
                sql_ids(auth_type="refresh_token"),
                [row.id for row in filter_account_rows(rows, auth_type="refresh_token")],
            )
            self.assertEqual(
                sql_ids(subscription_type="plus,free"),
                [row.id for row in filter_account_rows(rows, subscription_type="plus,free")],
            )
            self.assertEqual(
                sql_ids(account_validity_filter="invalid"),
                [row.id for row in filter_account_rows(rows, account_validity_filter="invalid")],
            )
            self.assertEqual(
                sql_ids(sub2api_state="unknown"),
                [row.id for row in filter_account_rows(rows, sub2api_state="unknown")],
            )
            self.assertEqual(
                sql_ids(revival_state="recovery_new"),
                [row.id for row in filter_account_rows(rows, revival_state="recovery_new")],
            )
            self.assertEqual(
                sql_ids(idea_submit_state="unavailable"),
                [row.id for row in filter_account_rows(rows, idea_submit_state="unavailable")],
            )
            self.assertEqual(
                sql_ids(idea_submit_state="paid,available"),
                [row.id for row in filter_account_rows(rows, idea_submit_state="paid,available")],
            )
            self.assertEqual(
                sql_ids(idea_submit_state="paid,unsubmitted"),
                [row.id for row in filter_account_rows(rows, idea_submit_state="paid,unsubmitted")],
            )

    def test_account_list_state_sql_generic_submission_matches_python_fallback(self):
        init_db()
        pix_failed = self._account(111)
        pix_failed.set_extra(
            {
                "idea_submit": {"status": "failed", "unavailable": True, "order_id": "pix-order"},
                "idea_submit_unavailable": True,
                "baxigpt_cdk": {"status": "failed", "order_id": "pix-order"},
                "chatgpt_last_payment_link": {"link_status": "pix_submitted"},
            }
        )
        pix_link_only = self._account(112)
        pix_link_only.set_extra(
            {"chatgpt_last_payment_link": {"link_status": "pix_submitted"}}
        )
        unavailable_only = self._account(113)
        unavailable_only.set_extra(
            {"idea_submit": {"unavailable": True}, "idea_submit_unavailable": True}
        )
        failed_before_submit = self._account(114)
        failed_before_submit.set_extra({"baxigpt_cdk": {"status": "failed"}})
        rows = [pix_failed, pix_link_only, unavailable_only, failed_before_submit]

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state"))
            session.exec(text("DELETE FROM accounts"))
            for row in rows:
                session.add(row)
            session.commit()
            refresh_account_list_state(session)

            cached = {
                int(item.account_id): (
                    item.idea_submit_state,
                    item.submit_state,
                    bool(item.has_submitted),
                )
                for item in session.exec(
                    select(AccountListStateModel).order_by(AccountListStateModel.account_id)
                ).all()
            }
            self.assertEqual(cached[111], ("unavailable", "failed", True))
            self.assertEqual(cached[112], ("available", "submitted", True))
            self.assertEqual(cached[113], ("unavailable", "unavailable", False))
            self.assertEqual(cached[114], ("failed", "failed", False))

            def sql_ids(**filters):
                query = select(AccountModel).join(
                    AccountListStateModel,
                    AccountListStateModel.account_id == AccountModel.id,
                )
                query = apply_account_list_state_filters(query, **filters)
                query = query.order_by(AccountModel.id.asc())
                return [int(row.id or 0) for row in session.exec(query).all()]

            for filters in (
                {"submit_state": "failed"},
                {"submit_state": "submitting"},
                {"has_submitted": True},
                {"has_submitted": False},
            ):
                self.assertEqual(
                    sql_ids(**filters),
                    [row.id for row in filter_account_rows(rows, **filters)],
                )

    def test_subscription_filter_uses_current_confirmed_plan_not_stale_snapshot(self):
        init_db()
        stale_plus = self._account(901)
        stale_plus.set_extra(
            {
                "chatgpt_capabilities": {
                    "subscription_plan": "plus",
                    "subscription_checked": False,
                },
                "chatgpt_local": {
                    "auth": {"state": "access_token_invalidated", "http_status": 401},
                    "subscription": {"plan": "unknown"},
                },
            }
        )
        current_plus = self._account(902)
        current_plus.set_extra(
            {
                "chatgpt_capabilities": {"subscription_plan": "plus", "subscription_checked": True},
                "chatgpt_local": {
                    "auth": {"state": "access_token_valid", "http_status": 200},
                    "subscription": {"plan": "plus"},
                },
            }
        )

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state"))
            session.exec(text("DELETE FROM accounts"))
            session.add(stale_plus)
            session.add(current_plus)
            session.commit()
            refresh_account_list_state(session)

            states = {
                int(row[0]): (row[1], row[2])
                for row in session.exec(
                    text(
                        """
                        SELECT account_id, subscription_type, account_validity
                        FROM account_list_state
                        WHERE account_id IN (901, 902)
                        ORDER BY account_id
                        """
                    )
                ).all()
            }

        self.assertEqual(states[901], ("unknown", "invalid"))
        self.assertEqual(states[902], ("plus", "valid"))

    def test_account_list_state_stale_refresh_only_updates_changed_rows(self):
        init_db()
        fresh = self._account(301)
        stale = self._account(302)
        stale.set_extra({"chatgpt_capabilities": {"subscription_plan": "free"}})

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state"))
            session.exec(text("DELETE FROM accounts"))
            session.add(fresh)
            session.add(stale)
            session.commit()
            refresh_account_list_state(session)

            fresh_source_updated_at = session.exec(
                text("SELECT CAST(updated_at AS TEXT) FROM accounts WHERE id = 301")
            ).one()
            fresh_source_updated_at = fresh_source_updated_at[0]
            session.exec(
                text(
                    """
                    UPDATE account_list_state
                    SET refreshed_at = 'keep-fresh',
                        source_updated_at = :source_updated_at
                    WHERE account_id = 301
                    """
                ),
                params={"source_updated_at": fresh_source_updated_at},
            )
            session.exec(
                text(
                    """
                    UPDATE account_list_state
                    SET refreshed_at = 'replace-stale',
                        source_updated_at = '2000-01-01 00:00:00'
                    WHERE account_id = 302
                    """
                )
            )
            session.commit()

            stale_row = session.get(AccountModel, 302)
            stale_row.set_extra({"chatgpt_local": {"subscription": {"plan": "plus"}}})
            stale_row.updated_at = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
            session.add(stale_row)
            session.commit()

            refreshed = refresh_stale_account_list_state(session)
            self.assertEqual(refreshed, 1)

            states = {
                int(row[0]): (row[1], row[2])
                for row in session.exec(
                    text(
                        """
                        SELECT account_id, subscription_type, refreshed_at
                        FROM account_list_state
                        WHERE account_id IN (301, 302)
                        ORDER BY account_id
                        """
                    )
                ).all()
            }

        self.assertEqual(states[301], ("unknown", "keep-fresh"))
        self.assertEqual(states[302][0], "plus")
        self.assertNotEqual(states[302][1], "replace-stale")

    def test_account_list_state_write_point_upsert_and_delete(self):
        init_db()
        row = self._account(401)
        row.set_extra({"manually_used": False})

        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state"))
            session.exec(text("DELETE FROM accounts"))
            session.add(row)
            session.commit()

            upsert_account_list_state_for_account_ids(session, [401])
            state = session.get(AccountListStateModel, 401)
            self.assertIsNotNone(state)
            self.assertFalse(bool(state.manually_used))

            account = session.get(AccountModel, 401)
            account.set_extra({"manually_used": True})
            account.updated_at = datetime(2026, 6, 17, 13, 0, 0, tzinfo=timezone.utc)
            session.add(account)
            upsert_account_list_state_for_account_ids(session, [401], commit=False)
            session.commit()

            state = session.get(AccountListStateModel, 401)
            self.assertTrue(bool(state.manually_used))

            delete_account_list_state_for_account_ids(session, [401])
            self.assertIsNone(session.get(AccountListStateModel, 401))

    def test_account_list_state_write_point_helpers_noop_for_mock_session(self):
        fake_session = mock.Mock()

        self.assertEqual(upsert_account_list_state_for_account_ids(fake_session, [999], commit=False), 0)
        self.assertEqual(delete_account_list_state_for_account_ids(fake_session, [999], commit=False), 0)

        fake_session.exec.assert_not_called()

    def test_account_list_state_schema_upgrade_adds_missing_columns(self):
        init_db()
        row = self._account(501)
        row.set_extra({"chatgpt_local": {"subscription": {"plan": "plus"}}})

        with Session(engine) as session:
            session.exec(text("DROP TABLE IF EXISTS account_list_state"))
            session.exec(
                text(
                    """
                    CREATE TABLE account_list_state (
                        account_id INTEGER PRIMARY KEY,
                        platform TEXT NOT NULL DEFAULT '',
                        manually_used INTEGER NOT NULL DEFAULT 0,
                        auth_type TEXT NOT NULL DEFAULT 'unknown'
                    )
                    """
                )
            )
            session.exec(text("DELETE FROM accounts"))
            session.add(row)
            session.commit()

            refreshed = refresh_account_list_state(session)
            columns = {
                str(info[1])
                for info in session.exec(text("PRAGMA table_info(account_list_state)")).all()
            }
            state = session.get(AccountListStateModel, 501)

        self.assertEqual(refreshed, 1)
        self.assertIn("source_updated_at", columns)
        self.assertIn("subscription_active_until_ts", columns)
        self.assertIn("idea_submit_state", columns)
        self.assertIn("submit_state", columns)
        self.assertIn("has_submitted", columns)
        self.assertIn("phone_binding_state", columns)
        self.assertIn("payment_link_platform", columns)
        self.assertIn("derivation_version", columns)
        self.assertEqual(state.subscription_type, "plus")

    def test_account_list_state_sql_sort_subscription_active_until_empty_last(self):
        init_db()
        rows = [
            self._account(201, "", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            self._account(202, "1781089634000", created_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
            self._account(203, "2026-05-01T00:00:00+00:00", created_at=datetime(2026, 1, 3, tzinfo=timezone.utc)),
            self._account(204, "1781089634", created_at=datetime(2026, 1, 4, tzinfo=timezone.utc)),
        ]
        with Session(engine) as session:
            session.exec(text("DELETE FROM account_list_state"))
            session.exec(text("DELETE FROM accounts"))
            for row in rows:
                session.add(row)
            session.commit()
            refresh_account_list_state(session)

            q = select(AccountModel).join(
                AccountListStateModel,
                AccountListStateModel.account_id == AccountModel.id,
            )
            asc_ids = [
                int(row.id or 0)
                for row in session.exec(
                    apply_account_list_state_sort(q, sort_by="subscription_active_until", sort_order="asc")
                ).all()
            ]
            desc_ids = [
                int(row.id or 0)
                for row in session.exec(
                    apply_account_list_state_sort(q, sort_by="subscription_active_until", sort_order="descend")
                ).all()
            ]
            expiry_asc_registration_desc_ids = [
                int(row.id or 0)
                for row in session.exec(
                    apply_account_list_state_sort(
                        q,
                        sort_by="subscription_active_until,created_at",
                        sort_order="asc,desc",
                    )
                ).all()
            ]
            default_ids = [
                int(row.id or 0)
                for row in session.exec(apply_account_list_state_sort(select(AccountModel))).all()
            ]

        self.assertEqual(asc_ids, [203, 202, 204, 201])
        self.assertEqual(desc_ids, [202, 204, 203, 201])
        self.assertEqual(expiry_asc_registration_desc_ids, [203, 204, 202, 201])
        self.assertEqual(default_ids, [201, 202, 203, 204])

    def test_init_db_ensures_registration_sort_index(self):
        init_db()
        with Session(engine) as session:
            indexes = {
                str(row[1])
                for row in session.exec(text("PRAGMA index_list(accounts)")).all()
            }

        self.assertIn("idx_accounts_platform_created_at_id", indexes)


if __name__ == "__main__":
    unittest.main()
