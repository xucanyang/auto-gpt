import base64
from datetime import datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.task_runtime import SkipCurrentAttemptRequested
from services.chatgpt_core.oauth_client import OAuthClient
from services.chatgpt_core.phone_service import (
    LocalPhoneGatewayService,
    SMSToMePhoneService,
    SharedPhoneGatewayService,
    UploadedPhoneService,
    parse_uploaded_phone_lines,
)
from services.chatgpt_core.utils import FlowState
from smstome_tool import PhoneEntry, parse_country_slugs


class OAuthCookieDecodeTests(unittest.TestCase):
    def test_decode_signed_cookie_payload(self):
        payload = {
            "email": "demo@example.com",
            "phone_number": "+447456344799",
            "phone_verification_channel": "whatsapp",
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
        cookie_value = f"{encoded}.sig-a.sig-b"

        self.assertEqual(OAuthClient._decode_cookie_json_value(cookie_value), payload)

    def test_decode_invalid_cookie_payload(self):
        self.assertIsNone(OAuthClient._decode_cookie_json_value("not-a-valid-cookie"))


class SMSToMeConfigTests(unittest.TestCase):
    def test_parse_country_slugs_accepts_csv_and_iterables(self):
        self.assertEqual(
            parse_country_slugs("united-kingdom, poland;finland"),
            ["united-kingdom", "poland", "finland"],
        )
        self.assertEqual(
            parse_country_slugs(["united-kingdom", "poland", "united_kingdom"]),
            ["united-kingdom", "poland"],
        )

    def test_phone_service_enabled_when_pool_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pool_path = Path(tmp_dir) / "phones.txt"
            pool_path.write_text("+447456344799\tunited-kingdom\thttps://example.com\n", encoding="utf-8")

            service = SMSToMePhoneService({"smstome_global_file": str(pool_path)})
            self.assertTrue(service.enabled)

    def test_phone_service_disabled_for_empty_pool_without_cookie(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pool_path = Path(tmp_dir) / "phones.txt"
            pool_path.write_text("", encoding="utf-8")

            service = SMSToMePhoneService({"smstome_global_file": str(pool_path)})
            self.assertFalse(service.enabled)

    def test_wait_for_code_forwards_cookie_timeout_and_poll_interval(self):
        entry = PhoneEntry(
            country_slug="united-kingdom",
            phone="+447456344799",
            detail_url="https://example.com/phone/1",
        )
        service = SMSToMePhoneService(
            {
                "smstome_cookie": "cf_clearance=demo",
                "smstome_otp_timeout_seconds": "66",
                "smstome_poll_interval_seconds": "7",
            }
        )

        with mock.patch("services.chatgpt_core.phone_service.wait_for_otp", return_value="123456") as mocked:
            code = service.wait_for_code(entry)

        self.assertEqual(code, "123456")
        mocked.assert_called_once()
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["cookie_header"], "cf_clearance=demo")
        self.assertEqual(kwargs["timeout"], 66)
        self.assertEqual(kwargs["poll_interval"], 7)
        self.assertTrue(callable(kwargs["stop_checker"]))
        self.assertFalse(kwargs["raise_on_timeout"])

    def test_ensure_pool_ready_syncs_with_configured_page_limit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pool_path = Path(tmp_dir) / "phones.txt"
            service = SMSToMePhoneService(
                {
                    "smstome_cookie": "cf_clearance=demo",
                    "smstome_country_slugs": "united-kingdom",
                    "smstome_global_file": str(pool_path),
                    "smstome_sync_max_pages_per_country": "9",
                }
            )

            with mock.patch("services.chatgpt_core.phone_service.update_global_phone_list", return_value=3) as mocked:
                service.ensure_pool_ready()

        mocked.assert_called_once()
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["cookie_header"], "cf_clearance=demo")
        self.assertEqual(kwargs["countries"], ["united-kingdom"])
        self.assertEqual(kwargs["output_path"], pool_path)
        self.assertEqual(kwargs["max_pages_per_country"], 9)


class LocalPhoneGatewayServiceTests(unittest.TestCase):
    def test_acquire_phone_uses_autogpt_serial_lane(self):
        service = LocalPhoneGatewayService(
            {
                "local_phone_gateway_url": "http://sms-gateway:8720",
                "local_phone_gateway_token": "token",
            }
        )

        with mock.patch.object(
            service,
            "_request",
            return_value={
                "ok": True,
                "claimed": True,
                "activation_id": "act_reserved",
                "phone": "+15550000001",
                "provider": "smsbower",
                "provider_activation_id": "123",
                "country": "美国",
                "source": "reserved_pool",
            },
        ) as request:
            entry = service.acquire_phone(email="demo@example.com")

        self.assertEqual(entry.activation_id, "act_reserved")
        self.assertEqual(entry.phone, "+15550000001")
        request.assert_called_once()
        self.assertEqual(request.call_args.args[:2], ("POST", "/api/v1/autogpt/phone-session/acquire"))
        self.assertTrue(request.call_args.kwargs["json_body"]["auto_acquire"])

    def test_acquire_phone_accepts_gateway_new_number_response(self):
        service = LocalPhoneGatewayService(
            {
                "local_phone_gateway_url": "http://sms-gateway:8720",
                "local_phone_gateway_token": "token",
            }
        )

        with mock.patch.object(
            service,
            "_request",
            return_value={
                "ok": True,
                "claimed": True,
                "activation_id": "act_new",
                "phone": "+15550000002",
                "provider": "smsbower",
                "provider_activation_id": "456",
                "country": "美国",
                "source": "new_number",
            },
        ) as request:
            entry = service.acquire_phone(email="demo@example.com")

        self.assertEqual(entry.activation_id, "act_new")
        self.assertEqual(entry.phone, "+15550000002")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[:2], ("POST", "/api/v1/autogpt/phone-session/acquire"))

    def test_acquire_phone_respects_disabled_auto_acquire(self):
        service = LocalPhoneGatewayService(
            {
                "local_phone_gateway_url": "http://sms-gateway:8720",
                "local_phone_gateway_token": "token",
                "local_phone_gateway_auto_acquire_enabled": "false",
            }
        )

        with mock.patch.object(
            service,
            "_request",
            return_value={"ok": True, "claimed": False, "message": "暂无待用号码"},
        ) as request:
            entry = service.acquire_phone(email="demo@example.com")

        self.assertIsNone(entry)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[:2], ("POST", "/api/v1/autogpt/phone-session/acquire"))
        self.assertFalse(request.call_args.kwargs["json_body"]["auto_acquire"])

    def test_acquire_phone_uses_short_queue_windows(self):
        service = LocalPhoneGatewayService(
            {
                "local_phone_gateway_url": "http://sms-gateway:8720",
                "local_phone_gateway_token": "token",
                "local_phone_gateway_queue_timeout_seconds": "30",
            }
        )

        with mock.patch.object(
            service,
            "_request",
            side_effect=[
                {"ok": True, "claimed": False, "queued": True, "message": "busy"},
                {
                    "ok": True,
                    "claimed": True,
                    "activation_id": "act_short",
                    "phone": "+15550000003",
                    "provider": "smsbower",
                    "country": "美国",
                    "source": "reuse_active",
                },
            ],
        ) as request:
            entry = service.acquire_phone(email="demo@example.com")

        self.assertEqual(entry.activation_id, "act_short")
        self.assertEqual(request.call_count, 2)
        self.assertLessEqual(request.call_args_list[0].kwargs["json_body"]["queue_timeout_seconds"], 5)
        self.assertLessEqual(request.call_args_list[0].kwargs["timeout"], 15)

    def test_shared_phone_gateway_reuses_activation_until_terminal_release(self):
        first = mock.Mock(phone="+15550000001", activation_id="act_shared", country_slug="美国")
        second = mock.Mock(phone="+15550000002", activation_id="act_second", country_slug="美国")
        base = mock.Mock()
        base.enabled = True
        base.max_attempts = 3
        base.max_resend_attempts = 20
        base.resend_interval_seconds = 30
        base.acquire_phone.side_effect = [first, second]
        base.prefix_hint.side_effect = lambda phone: str(phone)[:7]
        base.request_next_code.return_value = True

        service = SharedPhoneGatewayService(base)

        self.assertEqual(service.acquire_phone(email="a@example.com"), first)
        service.complete(first)
        self.assertEqual(service.acquire_phone(email="b@example.com"), first)
        service.cancel(first, reason="limit")
        self.assertEqual(service.acquire_phone(email="c@example.com"), second)

        self.assertEqual(base.acquire_phone.call_count, 2)
        base.request_next_code.assert_called_once_with(first)
        base.complete.assert_not_called()
        base.cancel.assert_called_once_with(first, reason="limit")

    def test_shared_phone_gateway_blacklist_releases_current_with_reason(self):
        first = mock.Mock(phone="+15550000001", activation_id="act_shared", country_slug="美国")
        base = mock.Mock()
        base.enabled = True
        base.max_attempts = 3
        base.max_resend_attempts = 20
        base.resend_interval_seconds = 30
        base.acquire_phone.return_value = first
        base.prefix_hint.side_effect = lambda phone: str(phone)[:7]

        service = SharedPhoneGatewayService(base)

        self.assertEqual(service.acquire_phone(email="a@example.com"), first)
        service.mark_blacklisted(
            first.phone,
            reason="This phone number is already linked to the maximum number of accounts.",
        )

        base.cancel.assert_called_once_with(
            first,
            reason="This phone number is already linked to the maximum number of accounts.",
        )
        self.assertIsNone(service.current_entry)

    def test_local_phone_gateway_blacklist_sends_reason_to_cancel(self):
        service = LocalPhoneGatewayService(
            {
                "local_phone_gateway_url": "http://sms-gateway:8720",
                "local_phone_gateway_token": "token",
            }
        )
        entry = mock.Mock(phone="+15550000001", activation_id="act_shared")
        service._entries_by_phone[entry.phone] = entry

        with mock.patch.object(service, "cancel") as cancel:
            service.mark_blacklisted(
                entry.phone,
                reason="This phone number is already linked to the maximum number of accounts.",
            )

        cancel.assert_called_once_with(
            entry,
            reason="This phone number is already linked to the maximum number of accounts.",
        )


class UploadedPhoneServiceTests(unittest.TestCase):
    def test_parse_uploaded_phone_lines_accepts_phone_api_pairs_and_dedupes(self):
        entries, errors = parse_uploaded_phone_lines(
            "\n".join(
                [
                    "+13434832954----https://api.sms8.net/api/record?token=one",
                    "+13434832954----https://api.sms8.net/api/record?token=duplicate",
                    "bad-line",
                ]
            )
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].phone, "+13434832954")
        self.assertEqual(entries[0].api_url, "https://api.sms8.net/api/record?token=one")
        self.assertEqual(len(errors), 2)
        self.assertIn("重复", errors[0]["reason"])
        self.assertIn("分隔符", errors[1]["reason"])

    def test_uploaded_phone_service_waits_until_sms8_code_field_is_present(self):
        entries, errors = parse_uploaded_phone_lines(
            "+13434832954----https://api.sms8.net/api/record?token=one"
        )
        self.assertFalse(errors)
        service = UploadedPhoneService(entries, {"uploaded_phone_poll_interval_seconds": "1"})
        service.bind_entry(entries[0])

        first = mock.Mock(status_code=200)
        first.json.return_value = {
            "code": 0,
            "msg": "No verification code",
            "data": {"code": "", "code_time": "", "expired_date": "2026-08-26 00:00:00"},
        }
        second = mock.Mock(status_code=200)
        second.json.return_value = {
            "code": 0,
            "msg": "OK",
            "data": {"code": "123456", "code_time": "2026-06-02 10:00:00"},
        }

        with mock.patch("services.chatgpt_core.phone_service.requests.get", side_effect=[first, second]):
            with mock.patch("services.chatgpt_core.phone_service.time.sleep"):
                self.assertEqual(service.wait_for_code(entries[0], timeout=10), "123456")
        self.assertEqual(service.last_expired_date, "2026-08-26 00:00:00")
        self.assertEqual(service.last_code_time, "2026-06-02 10:00:00")

    def test_uploaded_phone_service_extracts_six_digit_code_from_sms_text(self):
        entries, errors = parse_uploaded_phone_lines(
            "+13434832954----https://api.sms8.net/api/record?token=one"
        )
        self.assertFalse(errors)
        service = UploadedPhoneService(entries, {"uploaded_phone_poll_interval_seconds": "1"})
        service.bind_entry(entries[0])

        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "msg": "OK",
            "data": {
                "code": "OpenAI verification code: 779632.",
                "code_time": "2026-06-02 20:27:44",
            },
        }

        with mock.patch("services.chatgpt_core.phone_service.requests.get", return_value=response):
            self.assertEqual(service.wait_for_code(entries[0], timeout=10), "779632")
        self.assertEqual(service.last_code_time, "2026-06-02 20:27:44")
        self.assertTrue(service.last_code_was_extracted)

    def test_uploaded_phone_service_ignores_code_older_than_sms_send(self):
        entries, errors = parse_uploaded_phone_lines(
            "+13434832954----https://api.sms8.net/api/record?token=one"
        )
        self.assertFalse(errors)
        logs: list[str] = []
        service = UploadedPhoneService(entries, {"uploaded_phone_poll_interval_seconds": "1"}, log_fn=logs.append)
        service.bind_entry(entries[0])

        sent_at = datetime(2026, 6, 5, 14, 14, 14, tzinfo=timezone.utc).timestamp()
        stale = mock.Mock(status_code=200)
        stale.json.return_value = {
            "code": 0,
            "msg": "OK",
            "data": {"code": "822496", "code_time": "2026-06-05 22:13:38"},
        }
        fresh = mock.Mock(status_code=200)
        fresh.json.return_value = {
            "code": 0,
            "msg": "OK",
            "data": {"code": "654321", "code_time": "2026-06-05 22:14:18"},
        }

        with mock.patch("services.chatgpt_core.phone_service.time.time", return_value=sent_at):
            service.mark_sms_sent(entries[0])
        with mock.patch("services.chatgpt_core.phone_service.requests.get", side_effect=[stale, fresh]):
            with mock.patch("services.chatgpt_core.phone_service.time.sleep"):
                self.assertEqual(service.wait_for_code(entries[0], timeout=10), "654321")

        self.assertIn("忽略旧验证码 822496", "\n".join(logs))
        self.assertIn("收到验证码 654321", "\n".join(logs))
        self.assertEqual(service.last_code, "654321")


class OAuthPhoneBlacklistTests(unittest.TestCase):
    def test_should_blacklist_explicit_phone_rejection(self):
        state = FlowState(
            page_type="add_phone",
            payload={"error": {"message": "phone number is invalid"}},
        )
        self.assertTrue(
            OAuthClient._should_blacklist_phone_failure(
                "add-phone/send 失败: 400 - phone number is invalid",
                state,
            )
        )

    def test_should_blacklist_phone_reached_account_limit(self):
        self.assertTrue(
            OAuthClient._should_blacklist_phone_failure(
                "add-phone/send 失败: 403 - This phone number is already linked to the maximum number of accounts."
            )
        )

    def test_should_not_blacklist_whatsapp_or_delivery_failures(self):
        self.assertFalse(
            OAuthClient._should_blacklist_phone_failure(
                "add_phone 已切到 whatsapp 通道，当前 SMSToMe 仅支持短信接码"
            )
        )
        self.assertFalse(
            OAuthClient._should_blacklist_phone_failure("手机号 +447000000001 未收到短信验证码")
        )

    def test_handle_add_phone_blacklists_explicitly_rejected_number(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        entry = PhoneEntry(
            country_slug="united-kingdom",
            phone="+447000000001",
            detail_url="https://example.com/phone/1",
        )
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.max_attempts = 1
        phone_service.acquire_phone.return_value = entry
        phone_service.prefix_hint.return_value = "+447000"

        with mock.patch("services.chatgpt_core.oauth_client.create_phone_service", return_value=phone_service):
            with mock.patch.object(
                client,
                "_send_phone_number",
                return_value=(False, None, "add-phone/send 失败: 400 - phone number is invalid"),
            ):
                state = client._handle_add_phone_verification(
                    "device-id",
                    "Mozilla/5.0",
                    None,
                    None,
                    FlowState(page_type="add_phone"),
                )

        self.assertIsNone(state)
        phone_service.mark_blacklisted.assert_called_once_with(
            entry.phone,
            reason="add-phone/send 失败: 400 - phone number is invalid",
        )
        self.assertIn("add_phone 阶段失败", client.last_error)

    def test_handle_add_phone_blacklists_account_limit_and_tries_next_number(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        first = PhoneEntry(
            country_slug="united-states",
            phone="+12254379214",
            detail_url="https://example.com/phone/1",
        )
        second = PhoneEntry(
            country_slug="united-states",
            phone="+13692011161",
            detail_url="https://example.com/phone/2",
        )
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.max_attempts = 2
        phone_service.max_resend_attempts = 0
        phone_service.resend_interval_seconds = 0
        phone_service.acquire_phone.side_effect = [first, second]
        phone_service.prefix_hint.side_effect = lambda phone: str(phone)[:7]
        phone_service.wait_for_code.return_value = "123456"

        next_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )
        done_state = FlowState(page_type="done", current_url="https://chatgpt.com/")
        limit_detail = (
            "add-phone/send 失败: 403 - "
            "This phone number is already linked to the maximum number of accounts."
        )

        with mock.patch("services.chatgpt_core.oauth_client.create_phone_service", return_value=phone_service):
            with mock.patch.object(
                client,
                "_send_phone_number",
                side_effect=[(False, None, limit_detail), (True, next_state, "")],
            ):
                with mock.patch.object(
                    client,
                    "_decode_oauth_session_cookie",
                    return_value={"phone_verification_channel": "sms", "phone_number": second.phone},
                ):
                    with mock.patch.object(
                        client,
                        "_validate_phone_otp",
                        return_value=(True, done_state, ""),
                    ):
                        state = client._handle_add_phone_verification(
                            "device-id",
                            "Mozilla/5.0",
                            None,
                            None,
                            FlowState(page_type="add_phone"),
                        )

        self.assertEqual(state, done_state)
        self.assertEqual(phone_service.acquire_phone.call_count, 2)
        self.assertEqual(
            phone_service.acquire_phone.call_args_list[1].kwargs["exclude_prefixes"],
            {"+122543"},
        )
        reason = phone_service.mark_blacklisted.call_args.kwargs["reason"]
        self.assertIn("手机号已达到 OpenAI 账号绑定上限", reason)
        self.assertIn("maximum number of accounts", reason)
        phone_service.cancel.assert_not_called()

    def test_handle_add_phone_preserves_rejected_phone_reason_when_next_number_unavailable(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        entry = PhoneEntry(
            country_slug="united-states",
            phone="+12254379214",
            detail_url="https://example.com/phone/1",
        )
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.max_attempts = 2
        phone_service.acquire_phone.side_effect = [
            entry,
            RuntimeError("SMSBower 取号失败: NO_NUMBERS"),
        ]
        phone_service.prefix_hint.return_value = "+122543"
        limit_detail = (
            "add-phone/send 失败: 403 - "
            "This phone number is already linked to the maximum number of accounts."
        )

        with mock.patch("services.chatgpt_core.oauth_client.create_phone_service", return_value=phone_service):
            with mock.patch.object(
                client,
                "_send_phone_number",
                return_value=(False, None, limit_detail),
            ):
                state = client._handle_add_phone_verification(
                    "device-id",
                    "Mozilla/5.0",
                    None,
                    None,
                    FlowState(page_type="add_phone"),
                )

        self.assertIsNone(state)
        self.assertIn("maximum number of accounts", client.last_error)
        self.assertIn("随后取新号失败", client.last_error)
        self.assertIn("NO_NUMBERS", client.last_error)

    def test_handle_add_phone_does_not_blacklist_whatsapp_channel(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        entry = PhoneEntry(
            country_slug="united-kingdom",
            phone="+447000000002",
            detail_url="https://example.com/phone/2",
        )
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.max_attempts = 1
        phone_service.acquire_phone.return_value = entry
        phone_service.prefix_hint.return_value = "+447000"

        next_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )

        with mock.patch("services.chatgpt_core.oauth_client.create_phone_service", return_value=phone_service):
            with mock.patch.object(client, "_send_phone_number", return_value=(True, next_state, "")):
                with mock.patch.object(
                    client,
                    "_decode_oauth_session_cookie",
                    return_value={
                        "phone_verification_channel": "whatsapp",
                        "phone_number": entry.phone,
                    },
                ):
                    state = client._handle_add_phone_verification(
                        "device-id",
                        "Mozilla/5.0",
                        None,
                        None,
                        FlowState(page_type="add_phone"),
                    )

        self.assertIsNone(state)
        phone_service.mark_blacklisted.assert_not_called()
        self.assertIn("whatsapp", client.last_error)

    def test_handle_add_phone_reuses_same_number_until_code_arrives(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        entry = PhoneEntry(
            country_slug="united-kingdom",
            phone="+447000000003",
            detail_url="https://example.com/phone/3",
        )
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.max_attempts = 1
        phone_service.max_resend_attempts = 3
        phone_service.resend_interval_seconds = 0
        phone_service.acquire_phone.return_value = entry
        phone_service.prefix_hint.return_value = "+447000"
        phone_service.wait_for_code.side_effect = [None, None, "123456"]
        phone_service.request_next_code.return_value = True

        next_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )
        done_state = FlowState(page_type="done", current_url="https://chatgpt.com/")

        with mock.patch("services.chatgpt_core.oauth_client.create_phone_service", return_value=phone_service):
            with mock.patch.object(client, "_send_phone_number", return_value=(True, next_state, "")):
                with mock.patch.object(
                    client,
                    "_decode_oauth_session_cookie",
                    return_value={"phone_verification_channel": "sms", "phone_number": entry.phone},
                ):
                    with mock.patch.object(client, "_resend_phone_otp", return_value=(True, "")) as resend:
                        with mock.patch.object(
                            client,
                            "_validate_phone_otp",
                            return_value=(True, done_state, ""),
                        ) as validate:
                            state = client._handle_add_phone_verification(
                                "device-id",
                                "Mozilla/5.0",
                                None,
                                None,
                                FlowState(page_type="add_phone"),
                            )

        self.assertEqual(state, done_state)
        self.assertEqual(resend.call_count, 2)
        self.assertEqual(phone_service.request_next_code.call_count, 2)
        validate.assert_called_once()
        self.assertEqual(validate.call_args.args[0], "123456")
        phone_service.cancel.assert_not_called()
        phone_service.complete.assert_called_once_with(entry)

    def test_handle_add_phone_sms_probe_stops_before_validate(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        entry = PhoneEntry(
            country_slug="united-kingdom",
            phone="+447000000005",
            detail_url="https://example.com/phone/5",
        )
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.max_attempts = 1
        phone_service.max_resend_attempts = 0
        phone_service.resend_interval_seconds = 0
        phone_service.validate_delay_seconds = 0
        phone_service.acquire_phone.return_value = entry
        phone_service.prefix_hint.return_value = "+447000"
        phone_service.wait_for_code.return_value = "123456"

        next_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )

        with mock.patch("services.chatgpt_core.oauth_client.create_phone_service", return_value=phone_service):
            with mock.patch.object(client, "_send_phone_number", return_value=(True, next_state, "")):
                with mock.patch.object(
                    client,
                    "_decode_oauth_session_cookie",
                    return_value={"phone_verification_channel": "sms", "phone_number": entry.phone},
                ):
                    with mock.patch.object(client, "_validate_phone_otp") as validate:
                        state = client._handle_add_phone_verification(
                            "device-id",
                            "Mozilla/5.0",
                            None,
                            None,
                            FlowState(page_type="add_phone"),
                            sms_probe_only=True,
                        )

        self.assertIsNone(state)
        validate.assert_not_called()
        phone_service.complete.assert_called_once_with(entry)
        self.assertIn("未提交验证码", client.last_error)

    def test_handle_add_phone_waits_between_same_number_resends(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        entry = PhoneEntry(
            country_slug="united-kingdom",
            phone="+447000000004",
            detail_url="https://example.com/phone/4",
        )
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.max_attempts = 1
        phone_service.max_resend_attempts = 1
        phone_service.resend_interval_seconds = 12
        phone_service.acquire_phone.return_value = entry
        phone_service.prefix_hint.return_value = "+447000"
        phone_service.wait_for_code.side_effect = [None, "654321"]
        phone_service.request_next_code.return_value = True

        next_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )
        done_state = FlowState(page_type="done", current_url="https://chatgpt.com/")

        with mock.patch("services.chatgpt_core.oauth_client.create_phone_service", return_value=phone_service):
            with mock.patch.object(client, "_send_phone_number", return_value=(True, next_state, "")):
                with mock.patch.object(
                    client,
                    "_decode_oauth_session_cookie",
                    return_value={"phone_verification_channel": "sms", "phone_number": entry.phone},
                ):
                    with mock.patch.object(client, "_sleep_before_phone_resend") as sleep_before_resend:
                        with mock.patch.object(client, "_resend_phone_otp", return_value=(True, "")):
                            with mock.patch.object(
                                client,
                                "_validate_phone_otp",
                                return_value=(True, done_state, ""),
                            ):
                                state = client._handle_add_phone_verification(
                                    "device-id",
                                    "Mozilla/5.0",
                                    None,
                                    None,
                                    FlowState(page_type="add_phone"),
                                )

        self.assertEqual(state, done_state)
        sleep_before_resend.assert_called_once_with(12)

    def test_existing_phone_pool_prefix_uses_first_four_digits_after_plus(self):
        client = OAuthClient(config={}, verbose=False)

        class _Record:
            def __init__(self, phone):
                self.phone_e164 = phone
                self.api_url = "https://relay.example.com/code"

        class _FakeRepo:
            def get(self, _phone):
                return None

            def list(self):
                return [_Record("+12509870220")]

        with mock.patch("services.chatgpt_core.phone_pool_repository.PhonePoolRepository", _FakeRepo):
            _record, code, message = client._lookup_existing_phone_pool_record("+12532241242")

        self.assertEqual(code, "bound_phone_not_in_pool_prefix")
        self.assertIn("非手机号池号段", message)

    def test_existing_phone_pool_sms_rejection_stops_before_resend_and_manual(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        record = mock.Mock(api_url="https://relay.example.com/code")
        detail = (
            "phone-otp/send(sms) 失败: 400 - "
            "We've detected suspicious behavior from phone numbers similar to yours. Please try again later."
        )

        with mock.patch(
            "services.chatgpt_core.bound_phone.upsert_chatgpt_bound_phone",
            return_value=None,
        ):
            with mock.patch.object(
                client,
                "_resolve_existing_phone_hint",
                return_value={"phone": "+18255850239", "source": "test", "masked": ""},
            ):
                with mock.patch.object(
                    client,
                    "_lookup_existing_phone_pool_record",
                    return_value=(record, "", "手机号 +18255850239 命中手机号池"),
                ):
                    with mock.patch.object(client, "_current_phone_otp_channel", return_value="whatsapp"):
                        with mock.patch.object(
                            client,
                            "_select_phone_otp_channel",
                            return_value=(False, None, detail),
                        ):
                            with mock.patch.object(client, "_resend_phone_otp") as resend:
                                with mock.patch.object(client, "_wait_for_manual_phone_otp") as manual:
                                    with mock.patch.object(
                                        client,
                                        "_record_existing_phone_pool_openai_rejected",
                                    ) as record_rejected:
                                        state = client._handle_existing_phone_otp_verification(
                                            "device-id",
                                            "Mozilla/5.0",
                                            None,
                                            None,
                                            FlowState(page_type="phone_otp_select_channel"),
                                            allow_existing_phone_verification=True,
                                        )

        self.assertIsNone(state)
        resend.assert_not_called()
        manual.assert_not_called()
        record_rejected.assert_called_once()
        self.assertIn("OpenAI 已拒绝发送 SMS", client.last_error)
        self.assertIn("+18255850239", client.last_error)

    def test_manual_phone_otp_disables_whatsapp_for_phone_pool_segment(self):
        captured = {}

        class _TaskControl:
            def wait_for_verification_code(self, **kwargs):
                captured.update(kwargs)
                try:
                    kwargs["action_handler"]("switch_channel", {"channel": "whatsapp"})
                except ValueError as exc:
                    captured["switch_error"] = str(exc)
                return "123456"

        client = OAuthClient(
            config={
                "_manual_phone_otp_enabled": True,
                "_manual_phone_otp_timeout_seconds": 60,
                "_task_control": _TaskControl(),
                "_task_attempt_id": 123,
            },
            verbose=False,
        )
        client._log = lambda _msg: None
        done_state = FlowState(page_type="done", current_url="https://chatgpt.com/")

        with mock.patch.object(client, "_select_phone_otp_channel") as select_channel:
            with mock.patch.object(
                client,
                "_validate_phone_otp",
                return_value=(True, done_state, ""),
            ):
                state = client._wait_for_manual_phone_otp(
                    phone="+18255850239",
                    masked="",
                    channel="whatsapp",
                    reason="手机号池号段",
                    device_id="device-id",
                    user_agent="Mozilla/5.0",
                    sec_ch_ua=None,
                    impersonate=None,
                    state=FlowState(page_type="phone_otp_verification"),
                    allow_whatsapp_channel=False,
                )

        self.assertEqual(state, done_state)
        select_channel.assert_not_called()
        self.assertEqual(captured["metadata"]["available_channels"], ["sms"])
        self.assertFalse(captured["metadata"]["can_switch_channel"])
        self.assertEqual(captured["metadata"]["channel"], "sms")
        self.assertEqual(captured["actions"], ["resend"])
        self.assertIn("不允许 WhatsApp", captured["switch_error"])

    def test_resend_phone_otp_uses_json_content_type(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        response = mock.Mock(status_code=200, text="", url="https://auth.openai.com/api/accounts/phone-otp/resend")
        client.session = mock.Mock()
        client.session.post.return_value = response

        ok, detail = client._resend_phone_otp(
            "device-id",
            "Mozilla/5.0",
            None,
            None,
            FlowState(page_type="phone_otp_verification", current_url="https://auth.openai.com/phone-verification"),
        )

        self.assertTrue(ok)
        self.assertEqual(detail, "")
        kwargs = client.session.post.call_args.kwargs
        self.assertEqual(kwargs["json"], {})
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")


if __name__ == "__main__":
    unittest.main()
