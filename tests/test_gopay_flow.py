import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LEGACY_APP_SRC = os.path.join(ROOT, "..", "_migrate_any_auto_register_local", "app-src", "app")
LEGACY_APP_SRC = os.path.abspath(LEGACY_APP_SRC)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if LEGACY_APP_SRC not in sys.path:
    sys.path.insert(1, LEGACY_APP_SRC)

from services.chatgpt_core import gopay_flow


class DummyAccount:
    access_token = "at-demo"
    cookies = ""
    email = "demo@example.com"
    extra = {}


class GoPayFlowTests(unittest.TestCase):
    def test_parse_checkout_url_accepts_hosted_url(self):
        cs_id, stripe_url = gopay_flow.parse_checkout_url("https://chatgpt.com/checkout/openai_llc/cs_live_123")
        self.assertEqual(cs_id, "cs_live_123")
        self.assertEqual(stripe_url, "https://checkout.stripe.com/c/pay/cs_live_123")

    def test_extract_processor_entity_from_chatgpt_checkout_url(self):
        entity = gopay_flow._extract_processor_entity(
            "https://chatgpt.com/checkout/openai_ie/cs_live_123",
        )
        self.assertEqual(entity, "openai_ie")

    def test_probe_chatgpt_checkout_amount_reads_total_summary_due(self):
        with mock.patch.object(
            gopay_flow.GoPayRunner,
            "_resolve_checkout",
            return_value="cs_live_123",
        ), mock.patch.object(
            gopay_flow.GoPayRunner,
            "_fetch_publishable_key",
            return_value="pk_live_1234567890",
        ), mock.patch.object(
            gopay_flow.GoPayRunner,
            "_stripe_init_checkout",
            return_value=(
                {"total_summary": {"due": 34900000}, "currency": "idr"},
                gopay_flow.STRIPE_VERSION_BASE,
                {"currency": "idr", "payment_method_types": ["card"]},
            ),
        ):
            probe = gopay_flow.probe_chatgpt_checkout_amount(
                DummyAccount(),
                checkout_url="https://chatgpt.com/checkout/openai_llc/cs_live_123",
                country="ID",
                currency="IDR",
            )

        self.assertEqual(probe["amount"], 34900000)
        self.assertEqual(probe["amount_text"], "34900000")
        self.assertEqual(probe["amount_source"], "total_summary.due")
        self.assertFalse(probe["amount_is_zero"])

    def test_resolve_checkout_creates_hosted_checkout_when_no_session_id_is_present(self):
        session = gopay_flow.GoPaySession(
            session_id="gp_test",
            account_id=1,
            email="demo@example.com",
            country="ID",
            currency="IDR",
            checkout_url="https://chatgpt.com/checkout/openai_llc/",
        )
        runner = gopay_flow.GoPayRunner.__new__(gopay_flow.GoPayRunner)
        runner.s = session
        runner.account = DummyAccount()
        runner.proxy = ""
        runner.profile = {"impersonate": "chrome146", "ua": "ua"}
        runner.ext = mock.Mock()

        with mock.patch.object(
            gopay_flow,
            "_create_hosted_checkout",
            return_value=("https://chatgpt.com/checkout/openai_llc/cs_live_abc123", "cs_live_abc123"),
        ):
            cs_id = gopay_flow.GoPayRunner._resolve_checkout(runner)

        self.assertEqual(cs_id, "cs_live_abc123")
        self.assertEqual(session.checkout_url, "https://chatgpt.com/checkout/openai_llc/cs_live_abc123")
        self.assertEqual(session.stripe_checkout_url, "https://checkout.stripe.com/c/pay/cs_live_abc123")
        self.assertEqual(session.processor_entity, "openai_llc")

    def test_chatgpt_approve_allows_blocked_to_continue_polling(self):
        session = gopay_flow.GoPaySession(
            session_id="gp_test",
            account_id=1,
            email="demo@example.com",
            processor_entity="openai_ie",
        )
        runner = gopay_flow.GoPayRunner.__new__(gopay_flow.GoPayRunner)
        runner.s = session
        runner.account = DummyAccount()
        runner.proxy = ""
        runner.profile = {"impersonate": "chrome146", "ua": "ua"}

        blocked = mock.Mock()
        blocked.raise_for_status.return_value = None
        blocked.json.return_value = {"result": "blocked", "detail": "pending manual state"}

        with mock.patch.object(gopay_flow, "_post_chatgpt_with_profile", return_value=blocked) as post:
            payload = gopay_flow.GoPayRunner._chatgpt_approve(runner, "cs_live_123")

        self.assertEqual(payload["result"], "blocked")
        self.assertEqual(
            post.call_args.kwargs["json_body"],
            {"checkout_session_id": "cs_live_123", "processor_entity": "openai_ie"},
        )

    def test_follow_redirect_scans_post_approve_payload_for_redirect(self):
        session = gopay_flow.GoPaySession(
            session_id="gp_test",
            account_id=1,
            email="demo@example.com",
        )
        runner = gopay_flow.GoPayRunner.__new__(gopay_flow.GoPayRunner)
        runner.s = session
        runner.account = DummyAccount()
        runner.profile = {"impersonate": "chrome146", "ua": "ua"}
        runner.ext = mock.Mock()

        poll = mock.Mock(status_code=200)
        poll.json.return_value = {
            "setup_intent": {
                "status": "requires_action",
                "next_action": {
                    "type": "redirect_to_url",
                    "redirect_to_url": {"url": "https://pm-redirects.stripe.com/authorize/acct/test"},
                },
            },
            "submission_attempt": {"state": "approved"},
        }
        runner.ext.get.return_value = poll

        with mock.patch.object(
            gopay_flow.GoPayRunner,
            "_fetch_pm_redirect_snap_token",
            return_value="11111111-1111-1111-1111-111111111111",
        ):
            snap = gopay_flow.GoPayRunner._follow_redirect_to_midtrans(runner, "cs_live_123", "pk_live")

        self.assertEqual(snap, "11111111-1111-1111-1111-111111111111")

    def test_stripe_update_payment_page_address_posts_tax_region_steps(self):
        session = gopay_flow.GoPaySession(
            session_id="gp_test",
            account_id=1,
            email="demo@example.com",
            country="ID",
            billing={
                "country": "ID",
                "line1": "Jl. M.H. Thamrin No. 1",
                "city": "Jakarta",
                "state": "DKI Jakarta",
                "postal_code": "10310",
            },
        )
        runner = gopay_flow.GoPayRunner.__new__(gopay_flow.GoPayRunner)
        runner.s = session
        runner.account = DummyAccount()
        runner.proxy = ""
        runner.profile = {"impersonate": "chrome146", "ua": "ua", "stripe_locale": "id"}
        runner.billing = dict(session.billing or {})
        runner.ext = mock.Mock()
        runner.ext.post.return_value.status_code = 200
        runner.ext.post.return_value.text = ""

        with mock.patch.object(gopay_flow.time, "sleep", return_value=None):
            gopay_flow.GoPayRunner._stripe_update_payment_page_address(
                runner,
                "cs_live_123",
                "pk_live",
                gopay_flow.STRIPE_VERSION_FULL,
                {"elements_options_client": {}},
            )

        self.assertEqual(runner.ext.post.call_count, 6)
        first_call = runner.ext.post.call_args_list[0]
        third_call = runner.ext.post.call_args_list[2]
        self.assertEqual(first_call.kwargs["data"]["tax_region[country]"], "ID")
        self.assertEqual(third_call.kwargs["data"]["tax_region[line1]"], "Jl. M.H. Thamrin No. 1")

    def test_start_until_otp_updates_address_before_required_approve(self):
        session = gopay_flow.GoPaySession(
            session_id="gp_test",
            account_id=1,
            email="demo@example.com",
            country="ID",
            billing={
                "country": "ID",
                "line1": "Jl. M.H. Thamrin No. 1",
                "city": "Jakarta",
                "state": "DKI Jakarta",
                "postal_code": "10310",
            },
        )
        runner = gopay_flow.GoPayRunner.__new__(gopay_flow.GoPayRunner)
        runner.s = session
        runner.account = DummyAccount()
        runner.proxy = ""
        runner.profile = {"impersonate": "chrome146", "ua": "ua", "time_on_page": 30000}
        runner.billing = dict(session.billing or {})
        runner.ext = mock.Mock()

        events: list[str] = []

        with mock.patch.object(gopay_flow.GoPayRunner, "_resolve_checkout", return_value="cs_live_123"), \
            mock.patch.object(gopay_flow.GoPayRunner, "_fetch_publishable_key", return_value="pk_live"), \
            mock.patch.object(
                gopay_flow.GoPayRunner,
                "_stripe_init_checkout",
                return_value=(
                    {"init_checksum": "ic"},
                    gopay_flow.STRIPE_VERSION_BASE,
                    {"config_id": "cfg", "elements_options_client": {}},
                ),
            ), \
            mock.patch.object(
                gopay_flow.GoPayRunner,
                "_stripe_update_payment_page_address",
                side_effect=lambda *args, **kwargs: events.append("address"),
            ), \
            mock.patch.object(
                gopay_flow.GoPayRunner,
                "_stripe_create_pm",
                side_effect=lambda *args, **kwargs: events.append("create_pm") or "pm_1",
            ), \
            mock.patch.object(
                gopay_flow.GoPayRunner,
                "_stripe_confirm",
                side_effect=lambda *args, **kwargs: events.append("confirm") or {
                    "submission_attempt": {"state": "requires_approval"},
                    "setup_intent": {
                        "status": "requires_merchant_approval",
                    }
                },
            ), \
            mock.patch.object(
                gopay_flow.GoPayRunner,
                "_chatgpt_approve",
                side_effect=lambda *args, **kwargs: events.append("approve") or {"result": "approved"},
            ), \
            mock.patch.object(
                gopay_flow.GoPayRunner,
                "_follow_redirect_to_midtrans",
                side_effect=lambda *args, **kwargs: events.append("follow") or "11111111-1111-1111-1111-111111111111",
            ), \
            mock.patch.object(gopay_flow.GoPayRunner, "_midtrans_load_transaction"), \
            mock.patch.object(gopay_flow.GoPayRunner, "_midtrans_init_linking", return_value="reference-1"), \
            mock.patch.object(gopay_flow.GoPayRunner, "_gopay_validate_reference"), \
            mock.patch.object(gopay_flow.GoPayRunner, "_gopay_user_consent"):
            gopay_flow.GoPayRunner.start_until_otp(runner, "62", "81234567890")

        self.assertEqual(events[:4], ["address", "create_pm", "confirm", "approve"])

    def test_start_until_otp_skips_approve_when_confirm_already_requires_action(self):
        session = gopay_flow.GoPaySession(
            session_id="gp_test",
            account_id=1,
            email="demo@example.com",
            country="ID",
            billing={
                "country": "ID",
                "line1": "Jl. M.H. Thamrin No. 1",
                "city": "Jakarta",
                "state": "DKI Jakarta",
                "postal_code": "10310",
            },
        )
        runner = gopay_flow.GoPayRunner.__new__(gopay_flow.GoPayRunner)
        runner.s = session
        runner.account = DummyAccount()
        runner.proxy = ""
        runner.profile = {"impersonate": "chrome146", "ua": "ua", "time_on_page": 30000}
        runner.billing = dict(session.billing or {})
        runner.ext = mock.Mock()

        events: list[str] = []
        confirm_payload = {
            "setup_intent": {
                "status": "requires_action",
                "next_action": {
                    "type": "redirect_to_url",
                    "redirect_to_url": {"url": "https://pm-redirects.stripe.com/authorize/acct/test"},
                },
            },
            "submission_attempt": {"state": "approved"},
        }

        with mock.patch.object(gopay_flow.GoPayRunner, "_resolve_checkout", return_value="cs_live_123"), \
            mock.patch.object(gopay_flow.GoPayRunner, "_fetch_publishable_key", return_value="pk_live"), \
            mock.patch.object(
                gopay_flow.GoPayRunner,
                "_stripe_init_checkout",
                return_value=(
                    {"init_checksum": "ic"},
                    gopay_flow.STRIPE_VERSION_BASE,
                    {"config_id": "cfg", "elements_options_client": {}},
                ),
            ), \
            mock.patch.object(gopay_flow.GoPayRunner, "_stripe_update_payment_page_address"), \
            mock.patch.object(gopay_flow.GoPayRunner, "_stripe_create_pm", return_value="pm_1"), \
            mock.patch.object(gopay_flow.GoPayRunner, "_stripe_confirm", return_value=confirm_payload), \
            mock.patch.object(
                gopay_flow.GoPayRunner,
                "_chatgpt_approve",
                side_effect=lambda *args, **kwargs: events.append("approve") or {"result": "approved"},
            ), \
            mock.patch.object(
                gopay_flow.GoPayRunner,
                "_follow_redirect_to_midtrans",
                side_effect=lambda *args, **kwargs: events.append("follow") or "11111111-1111-1111-1111-111111111111",
            ), \
            mock.patch.object(gopay_flow.GoPayRunner, "_midtrans_load_transaction"), \
            mock.patch.object(gopay_flow.GoPayRunner, "_midtrans_init_linking", return_value="reference-1"), \
            mock.patch.object(gopay_flow.GoPayRunner, "_gopay_validate_reference"), \
            mock.patch.object(gopay_flow.GoPayRunner, "_gopay_user_consent"):
            gopay_flow.GoPayRunner.start_until_otp(runner, "62", "81234567890")

        self.assertEqual(events, ["follow"])

    def test_create_gopay_session_without_checkout_url_keeps_session_active(self):
        class ImmediateThread:
            def __init__(self, target, daemon=False):
                self.target = target
                self.daemon = daemon

            def start(self):
                self.target()

        with mock.patch.object(gopay_flow.threading, "Thread", ImmediateThread), \
            mock.patch.object(gopay_flow.GoPayRunner, "start_until_otp", return_value=None):
            snapshot = gopay_flow.create_gopay_session(
                1,
                DummyAccount(),
                plan="plus",
                country="ID",
                currency="IDR",
                proxy="",
                phone_country_code="62",
                phone_number="81234567890",
            )

        self.assertEqual(snapshot["status"], "active")
        self.assertEqual(snapshot["plan"], "plus")

    def test_browser_profile_locale_follows_checkout_country(self):
        cases = (
            ("ID", "id-ID", "id"),
            ("SG", "en-SG", "en"),
            ("US", "en-US", "en"),
            ("GB", "en-GB", "en"),
        )
        for country, expected_locale, expected_stripe_locale in cases:
            with self.subTest(country=country):
                session = gopay_flow.GoPaySession(
                    session_id=f"gp_{country.lower()}",
                    account_id=1,
                    email="demo@example.com",
                    country=country,
                    browser_profile={
                        "name": "custom",
                        "impersonate": "chrome146",
                        "ua": "ua",
                        "locale": "id-ID",
                        "stripe_locale": "id",
                    },
                )

                profile = gopay_flow._select_gopay_browser_profile(session, DummyAccount())

                self.assertEqual(profile["locale"], expected_locale)
                self.assertEqual(profile["stripe_locale"], expected_stripe_locale)
                self.assertEqual(session.browser_profile["locale"], expected_locale)

    def test_fetch_publishable_key_continues_past_first_rejected_key(self):
        session = gopay_flow.GoPaySession(
            session_id="gp_test",
            account_id=1,
            email="demo@example.com",
        )
        runner = gopay_flow.GoPayRunner.__new__(gopay_flow.GoPayRunner)
        runner.s = session
        runner.account = DummyAccount()
        runner.proxy = ""
        runner.profile = {"impersonate": "chrome146", "ua": "ua", "stripe_locale": "id", "locale": "id-ID"}
        runner.ext = mock.Mock()

        first = mock.Mock(status_code=400, text="No such checkout.session")
        second = mock.Mock(status_code=200, text="{}")
        runner.ext.post.side_effect = [first, second]

        with mock.patch.object(gopay_flow, "DEFAULT_STRIPE_PK", "pk_test_demo"), \
            mock.patch.object(gopay_flow.time, "sleep", return_value=None):
            key = gopay_flow.GoPayRunner._fetch_publishable_key(runner, "cs_live_123")

        self.assertEqual(key, "pk_live_51Pj377KslHRdbaPgTJYjThzH3f5dt1N1vK7LUp0qh0yNSarhfZ6nfbG7FFlh8KLxVkvdMWN5o6Mc4Vda6NHaSnaV00C2Sbl8Zs")
        self.assertEqual(runner.ext.post.call_count, 2)

    def test_midtrans_init_linking_retries_429_with_retry_after(self):
        session = gopay_flow.GoPaySession(
            session_id="gp_test",
            account_id=1,
            email="demo@example.com",
        )
        runner = gopay_flow.GoPayRunner.__new__(gopay_flow.GoPayRunner)
        runner.s = session
        runner.account = DummyAccount()
        runner.profile = {"impersonate": "chrome146", "ua": "ua"}
        runner.ext = mock.Mock()

        limited = mock.Mock(status_code=429, text="", headers={"Retry-After": "3"})
        limited.json.side_effect = ValueError("no json")
        fallback_limited = mock.Mock(status_code=429, text="", headers={})
        fallback_limited.json.return_value = {"error_messages": ["too many request"]}
        linked = mock.Mock(status_code=201)
        linked.json.return_value = {
            "activation_link_url": "https://example.test/callback?reference=11111111-1111-1111-1111-111111111111",
        }
        runner.ext.post.side_effect = [limited, fallback_limited, linked]

        with mock.patch.object(gopay_flow.time, "sleep", return_value=None) as sleep:
            reference_id = gopay_flow.GoPayRunner._midtrans_init_linking(
                runner,
                "22222222-2222-2222-2222-222222222222",
                "62",
                "81234567890",
            )

        self.assertEqual(reference_id, "11111111-1111-1111-1111-111111111111")
        sleep.assert_called_once_with(3.0)
        self.assertEqual(runner.ext.post.call_count, 3)

    def test_midtrans_init_linking_uses_no_auth_fallback_after_429(self):
        session = gopay_flow.GoPaySession(
            session_id="gp_test",
            account_id=1,
            email="demo@example.com",
        )
        runner = gopay_flow.GoPayRunner.__new__(gopay_flow.GoPayRunner)
        runner.s = session
        runner.account = DummyAccount()
        runner.profile = {"impersonate": "chrome146", "ua": "ua"}
        runner.ext = mock.Mock()

        limited = mock.Mock(status_code=429, text="", headers={})
        limited.json.return_value = {"error_messages": ["too many request"]}
        linked = mock.Mock(status_code=201)
        linked.json.return_value = {
            "activation_link_url": "https://merchants-gws-app.gopayapi.com/linking?reference=11111111-1111-1111-1111-111111111111",
        }
        runner.ext.post.side_effect = [limited, linked]

        with mock.patch.object(gopay_flow.time, "sleep", return_value=None) as sleep:
            reference_id = gopay_flow.GoPayRunner._midtrans_init_linking(
                runner,
                "22222222-2222-2222-2222-222222222222",
                "62",
                "81234567890",
            )

        self.assertEqual(reference_id, "11111111-1111-1111-1111-111111111111")
        first_headers = runner.ext.post.call_args_list[0].kwargs["headers"]
        fallback_headers = runner.ext.post.call_args_list[1].kwargs["headers"]
        self.assertIn("Authorization", first_headers)
        self.assertNotIn("Authorization", fallback_headers)
        sleep.assert_not_called()

    def test_resend_otp_posts_reference_and_updates_snapshot(self):
        session = gopay_flow.GoPaySession(
            session_id="gp_test",
            account_id=1,
            email="demo@example.com",
            phase=gopay_flow.PHASE_WAITING_OTP,
            reference_id="11111111-1111-1111-1111-111111111111",
        )
        runner = gopay_flow.GoPayRunner.__new__(gopay_flow.GoPayRunner)
        runner.s = session
        runner.profile = {"stripe_locale": "id"}
        runner.ext = mock.Mock()
        response = mock.Mock(status_code=200)
        response.json.return_value = {"success": True}
        runner.ext.post.return_value = response

        gopay_flow.GoPayRunner.resend_otp(runner)

        runner.ext.post.assert_called_once()
        call = runner.ext.post.call_args
        self.assertEqual(call.args[0], "https://gwa.gopayapi.com/v1/linking/resend-otp")
        self.assertEqual(call.kwargs["json"], {"reference_id": "11111111-1111-1111-1111-111111111111"})
        self.assertEqual(session.otp_resend_count, 1)
        self.assertTrue(session.last_otp_resend_at)

    def test_auto_resend_scheduler_only_fires_while_waiting_for_otp(self):
        session = gopay_flow.GoPaySession(
            session_id="gp_test",
            account_id=1,
            email="demo@example.com",
            phase=gopay_flow.PHASE_WAITING_OTP,
            reference_id="11111111-1111-1111-1111-111111111111",
            otp_auto_resend_delay_seconds=5,
        )
        runner = mock.Mock()
        gopay_flow._set_runner(session, runner)

        class ImmediateThread:
            def __init__(self, target, daemon=False):
                self.target = target
                self.daemon = daemon

            def start(self):
                self.target()

        with mock.patch.object(gopay_flow.threading, "Thread", ImmediateThread), \
            mock.patch.object(gopay_flow.time, "sleep", return_value=None):
            gopay_flow._schedule_otp_auto_resend(session)

        runner.resend_otp.assert_called_once_with(auto=True)


if __name__ == "__main__":
    unittest.main()
