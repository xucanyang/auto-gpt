import types
import unittest
from unittest import mock

from services.chatgpt_core.chatgpt_registration_mode_adapter import (
    CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
    resolve_chatgpt_registration_mode,
)


def _result(**overrides):
    values = {
        "success": True,
        "email": "demo@example.com",
        "password": "pw",
        "account_id": "acct-demo",
        "workspace_id": "acct-demo",
        "access_token": "at-demo",
        "refresh_token": "",
        "id_token": "",
        "session_token": "session-demo",
        "source": "register",
        "metadata": {},
        "logs": [],
        "error_message": "",
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


class ChatGPTRegistrationModeAdapterTests(unittest.TestCase):
    def test_resolve_defaults_to_access_token_only_mode(self):
        self.assertEqual(
            resolve_chatgpt_registration_mode({}),
            CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
        )

    def test_resolve_supports_boolean_no_rt_flag(self):
        self.assertEqual(
            resolve_chatgpt_registration_mode(
                {"chatgpt_has_refresh_token_solution": False}
            ),
            CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
        )

    def test_build_account_uses_single_current_account_credentials(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )

        account = adapter.build_account(
            _result(
                workspace_id="ws-personal",
                id_token="id-demo",
                metadata={
                    "cookies": "oai-did=device",
                    "cookie_header": "oai-did=device",
                },
            ),
            fallback_password="fallback",
        )

        self.assertEqual(account.email, "demo@example.com")
        self.assertEqual(account.password, "pw")
        self.assertEqual(account.token, "at-demo")
        self.assertEqual(account.user_id, "acct-demo")
        self.assertEqual(account.extra["refresh_token"], "")
        self.assertEqual(account.extra["account_id"], "acct-demo")
        self.assertEqual(account.extra["workspace_id"], "ws-personal")
        self.assertEqual(account.extra["cookies"], "oai-did=device")
        self.assertEqual(
            account.extra["chatgpt_registration_mode"],
            CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
        )
        self.assertFalse(account.extra["chatgpt_has_refresh_token_solution"])

    def test_build_account_propagates_checkout_skip_save_metadata(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )

        account = adapter.build_account(
            _result(
                metadata={
                    "chatgpt_checkout_url": "https://chatgpt.com/checkout/openai_llc/cs_live_123",
                    "chatgpt_checkout_amount": "34900000",
                    "chatgpt_checkout_amount_is_zero": False,
                    "chatgpt_skip_save_account": True,
                    "chatgpt_skip_save_reason": "Plus checkout amount != 0",
                }
            ),
            fallback_password="fallback",
        )

        self.assertTrue(account.extra["chatgpt_skip_save_account"])
        self.assertEqual(account.extra["chatgpt_checkout_amount"], "34900000")
        self.assertEqual(
            account.extra["cashier_url"],
            "https://chatgpt.com/checkout/openai_llc/cs_live_123",
        )

    def test_build_account_propagates_gopay_provider_link_metadata(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )

        account = adapter.build_account(
            _result(
                metadata={
                    "chatgpt_gopay_provider_link_enabled": True,
                    "chatgpt_gopay_provider_link_ready": True,
                    "chatgpt_gopay_provider_link": "https://app.midtrans.com/snap/v4/redirection/demo",
                    "chatgpt_gopay_provider_link_cs_id": "cs_live_123",
                    "chatgpt_gopay_provider_link_payment_method_types": ["gopay"],
                }
            ),
            fallback_password="fallback",
        )

        self.assertTrue(account.extra["chatgpt_gopay_provider_link_enabled"])
        self.assertTrue(account.extra["chatgpt_gopay_provider_link_ready"])
        self.assertEqual(
            account.extra["chatgpt_gopay_provider_link"],
            "https://app.midtrans.com/snap/v4/redirection/demo",
        )
        self.assertEqual(
            account.extra["chatgpt_gopay_provider_link_payment_method_types"],
            ["gopay"],
        )

    def test_build_account_marks_already_paid_checkout_as_invalid(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )

        account = adapter.build_account(
            _result(
                email="paid@example.com",
                metadata={
                    "chatgpt_checkout_error_code": "already_paid",
                    "chatgpt_account_unavailable": True,
                    "chatgpt_payment_already_paid": True,
                    "chatgpt_skip_save_account": True,
                    "chatgpt_skip_save_reason": "Plus checkout already paid",
                },
            ),
            fallback_password="fallback",
        )

        self.assertEqual(account.status.value, "invalid")
        self.assertTrue(account.extra["chatgpt_payment_already_paid"])
        self.assertTrue(account.extra["chatgpt_skip_save_account"])

    def test_build_account_marks_registration_access_token_as_partial_auth(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )

        account = adapter.build_account(
            _result(
                source="registration_session",
                metadata={"registration_access_token_saved": True},
            ),
            fallback_password="fallback",
        )

        self.assertEqual(account.token, "at-demo")
        self.assertEqual(account.status.value, "pending_payment")
        self.assertEqual(account.extra["chatgpt_token_source"], "registration_session")
        self.assertEqual(account.extra["auth_level"], "access_token_only")
        self.assertTrue(account.extra["partial_auth"])

    def test_build_account_carries_full_auth_failure_metadata(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )

        account = adapter.build_account(
            _result(
                source="registration_session",
                metadata={
                    "registration_access_token_saved": True,
                    "registration_full_auth_failed": True,
                    "registration_full_auth_error": "add_phone required",
                    "needs_auth_capture": True,
                    "chatgpt_phone_challenge": {
                        "type": "add_phone",
                        "status": "unbound_required",
                    },
                },
            ),
            fallback_password="fallback",
        )

        self.assertEqual(account.status.value, "pending_payment")
        self.assertTrue(account.extra["needs_auth_capture"])
        self.assertTrue(account.extra["auth_capture_required"])
        self.assertTrue(account.extra["registration_full_auth_failed"])
        self.assertEqual(
            account.extra["registration_full_auth_error"],
            "add_phone required",
        )
        self.assertEqual(account.extra["chatgpt_phone_challenge"]["type"], "add_phone")

    def test_build_account_marks_registered_without_tokens_as_auth_pending(self):
        for registration_mode in ("access_token_only", "refresh_token"):
            with self.subTest(registration_mode=registration_mode):
                adapter = build_chatgpt_registration_mode_adapter(
                    {"chatgpt_registration_mode": registration_mode}
                )
                account = adapter.build_account(
                    _result(
                        account_id="",
                        workspace_id="",
                        access_token="",
                        refresh_token="",
                        session_token="",
                        source="registered_auth_pending",
                        metadata={
                            "registered_auth_pending": True,
                            "needs_auth_capture": True,
                            "registration_full_auth_failed": True,
                            "registration_full_auth_error": "browser auth capture failed",
                        },
                    ),
                    fallback_password="fallback",
                )

                self.assertEqual(account.status.value, "pending_payment")
                self.assertEqual(account.token, "")
                self.assertEqual(account.user_id, "")
                self.assertEqual(account.extra["auth_level"], "registered_auth_pending")
                self.assertTrue(account.extra["partial_auth"])
                self.assertEqual(
                    account.extra["registration_full_auth_failed_policy"],
                    "keep_registered_auth_pending",
                )

    def test_build_account_persists_effective_executor_and_stage_transports(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        account = adapter.build_account(
            _result(
                metadata={
                    "registration_context": {
                        "requested_executor": "headless",
                        "effective_executor": "headless",
                        "registration_transport": "camoufox_browser",
                        "stage_transports": [
                            {
                                "stage": "registration",
                                "transport": "camoufox_browser",
                                "executor": "headless",
                            }
                        ],
                    }
                }
            ),
            fallback_password="fallback",
        )

        self.assertEqual(account.extra["requested_executor_type"], "headless")
        self.assertEqual(account.extra["effective_executor_type"], "headless")
        self.assertEqual(
            account.extra["chatgpt_registration_transport"],
            "camoufox_browser",
        )
        self.assertEqual(
            account.extra["chatgpt_registration_stage_transports"][0]["stage"],
            "registration",
        )

    def test_build_account_carries_confirmed_phone_binding_metadata(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        binding = {
            "phone": "+16134655704",
            "status": "bound",
            "source": "oauth_add_phone",
            "bound_at": "2026-07-16T00:00:00+00:00",
        }

        account = adapter.build_account(
            _result(
                refresh_token="rt-demo",
                metadata={
                    "chatgpt_phone_binding": binding,
                    "chatgpt_phone_binding_history": [binding],
                    "chatgpt_bound_phone": {
                        "phone": "+16134655704",
                        "verification_status": "verified",
                    },
                    "chatgpt_bound_phone_number": "+16134655704",
                },
            ),
            fallback_password="fallback",
        )

        self.assertEqual(account.extra["chatgpt_phone_binding"]["status"], "bound")
        self.assertEqual(account.extra["chatgpt_phone_binding_history"][-1]["phone"], "+16134655704")
        self.assertEqual(account.extra["chatgpt_bound_phone_number"], "+16134655704")

    def test_access_token_only_adapter_passes_runtime_context_to_engine(self):
        created = {}

        class FakeEngine:
            def __init__(self, **kwargs):
                created["kwargs"] = kwargs
                self.email = None
                self.password = None

            def run(self):
                created["email"] = self.email
                created["password"] = self.password
                return _result()

        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        context = ChatGPTRegistrationContext(
            email_service=object(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda _msg: None,
            email="demo@example.com",
            password="pw-demo",
            browser_mode="headed",
            max_retries=5,
            extra_config={"register_max_retries": 5},
        )
        fake_module = types.ModuleType(
            "services.chatgpt_core.access_token_only_registration_engine"
        )
        fake_module.AccessTokenOnlyRegistrationEngine = FakeEngine

        with mock.patch.dict(
            "sys.modules",
            {"services.chatgpt_core.access_token_only_registration_engine": fake_module},
        ):
            adapter.run(context)

        self.assertEqual(created["email"], "demo@example.com")
        self.assertEqual(created["password"], "pw-demo")
        self.assertEqual(created["kwargs"]["browser_mode"], "headed")
        self.assertEqual(created["kwargs"]["max_retries"], 5)

    def test_refresh_token_stage2_captures_full_auth_for_same_single_account(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        context = ChatGPTRegistrationContext(
            email_service=mock.Mock(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda _msg, *_: None,
            email=None,
            password="pw",
            browser_mode="protocol",
            max_retries=1,
            extra_config={"chatgpt_registration_mode": "refresh_token"},
        )
        register_client = types.SimpleNamespace(
            device_id="device-stage1",
            ua="UA",
            sec_ch_ua='"Chromium";v="136"',
            impersonate="chrome136",
            fingerprint={"device_id": "device-stage1"},
        )
        stage1_engine = types.SimpleNamespace(_last_chatgpt_client=register_client)
        stage1_result = _result(
            email="stage1@example.com",
            password="pw-stage1",
            account_id="acct-personal",
            workspace_id="ws-personal",
            access_token="at-stage1",
            session_token="session-stage1",
            metadata={
                "mailbox_state": {"provider": "icloud_hme"},
                "cookies": "oai-did=device",
            },
        )
        captured = {}

        class FakeRegistrationResult:
            def __init__(self, **kwargs):
                defaults = {
                    "success": False,
                    "email": "",
                    "password": "",
                    "account_id": "",
                    "workspace_id": "",
                    "access_token": "",
                    "refresh_token": "",
                    "id_token": "",
                    "session_token": "",
                    "source": "register",
                    "error_message": "",
                    "logs": [],
                    "metadata": {},
                }
                defaults.update(kwargs)
                self.__dict__.update(defaults)

        class FakeStage2Engine:
            def __init__(self, **kwargs):
                captured["engine_kwargs"] = kwargs
                self.email_service = kwargs["email_service"]
                self.email = None
                self.password = None
                self.logs = []

            def _log(self, message, _level="info"):
                self.logs.append(str(message))

            def _capture_auth_via_fresh_login(self, **kwargs):
                captured["auth_kwargs"] = kwargs
                return {
                    "account_id": "acct-personal",
                    "workspace_id": "ws-personal",
                    "access_token": "at-full",
                    "refresh_token": "rt-full",
                    "id_token": "id-full",
                    "session_token": "",
                    "source": "registration_stage2_full_auth",
                }

            @staticmethod
            def _auth_payload_has_refresh_token(payload):
                return bool(payload.get("refresh_token"))

            @staticmethod
            def _apply_auth_payload_to_result(result, payload):
                result.success = True
                for key, value in payload.items():
                    setattr(result, key, value)

            @staticmethod
            def _append_gopay_provider_link_metadata(_result, _session_result):
                return None

        fake_module = types.ModuleType(
            "services.chatgpt_core.refresh_token_registration_engine"
        )
        fake_module.RefreshTokenRegistrationEngine = FakeStage2Engine
        fake_module.RegistrationResult = FakeRegistrationResult
        fake_module.EmailServiceAdapter = (
            lambda email_service, email, log_fn: types.SimpleNamespace(
                email_service=email_service,
                email=email,
                log_fn=log_fn,
            )
        )

        with mock.patch.dict(
            "sys.modules",
            {"services.chatgpt_core.refresh_token_registration_engine": fake_module},
        ):
            result = adapter._capture_stage2_from_stage1_session(
                context=context,
                stage1_engine=stage1_engine,
                stage1_result=stage1_result,
                stage2_extra={"chatgpt_registration_mode": "refresh_token"},
                saved_stage1_id=42,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.email, "stage1@example.com")
        self.assertEqual(result.account_id, "acct-personal")
        self.assertEqual(result.workspace_id, "ws-personal")
        self.assertEqual(result.refresh_token, "rt-full")
        self.assertEqual(result.metadata["registration_stage1_saved_account_id"], 42)
        self.assertEqual(
            captured["auth_kwargs"]["login_source"],
            "registration_stage2_full_auth",
        )
        self.assertEqual(captured["auth_kwargs"]["device_id"], "device-stage1")

    def test_browser_refresh_token_stage2_uses_browser_oauth_only(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        context = ChatGPTRegistrationContext(
            email_service=mock.Mock(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda _msg, *_: None,
            email=None,
            password="pw",
            browser_mode="headless",
            max_retries=1,
            extra_config={"chatgpt_registration_mode": "refresh_token"},
        )
        register_client = types.SimpleNamespace(
            device_id="device-stage1",
            registration_stage_transports=[
                {
                    "stage": "registration",
                    "transport": "camoufox_browser",
                    "executor": "headless",
                }
            ],
        )
        stage1_engine = mock.Mock()
        stage1_engine._last_chatgpt_client = register_client
        stage1_engine._capture_browser_oauth_tokens.return_value = (
            True,
            {
                "account_id": "acct-browser",
                "access_token": "at-browser",
                "refresh_token": "rt-browser",
                "id_token": "id-browser",
            },
        )
        stage1_engine._build_registration_context_payload.return_value = {
            "requested_executor": "headless",
            "effective_executor": "headless",
            "registration_transport": "camoufox_browser",
            "stage_transports": register_client.registration_stage_transports,
            "first_name": "Fixed",
            "last_name": "Profile",
            "birthdate": "1990-01-02",
            "browser_runtime_profile": {
                "browser_family": "camoufox",
                "device_id": "device-stage1",
                "user_agent": "Mozilla/5.0 Firefox/135.0",
            },
        }
        stage1_result = _result(
            email="stage1@example.com",
            password="pw-stage1",
            access_token="at-stage1",
            metadata={
                "chatgpt_browser_runtime_profile": {
                    "browser_family": "camoufox",
                    "device_id": "device-stage1",
                    "user_agent": "Mozilla/5.0 Firefox/135.0",
                },
                "registration_context": {
                    "first_name": "Fixed",
                    "last_name": "Profile",
                    "birthdate": "1990-01-02",
                }
            },
        )

        with mock.patch(
            "services.chatgpt_core.refresh_token_registration_engine.RefreshTokenRegistrationEngine._capture_auth_via_fresh_login",
            side_effect=AssertionError("browser executor used protocol OAuth"),
        ) as protocol_oauth, mock.patch(
            "services.chatgpt_core.refresh_token_registration_engine.RefreshTokenRegistrationEngine._append_gopay_provider_link_metadata",
        ):
            result = adapter._capture_stage2_from_stage1_session(
                context=context,
                stage1_engine=stage1_engine,
                stage1_result=stage1_result,
                stage2_extra={"chatgpt_registration_mode": "refresh_token"},
                saved_stage1_id=42,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.refresh_token, "rt-browser")
        self.assertEqual(
            result.metadata["auth_capture_method"],
            "registration_stage2_browser_oauth",
        )
        self.assertEqual(
            result.metadata["chatgpt_browser_runtime_profile"]["browser_family"],
            "camoufox",
        )
        protocol_oauth.assert_not_called()
        stage1_engine._capture_browser_oauth_tokens.assert_called_once()

    def test_legacy_refresh_token_mode_runs_signup_once_without_auth_capture(self):
        stage1_result = _result(
            email="stage1@example.com",
            password="pw-stage1",
            access_token="at-stage1",
            refresh_token="",
            session_token="session-stage1",
        )

        class FakeStage1Engine:
            run_calls = 0

            def __init__(self, **kwargs):
                self.email = None
                self.password = None
                self.extra_config = kwargs["extra_config"]

            def run(self):
                self.run_calls += 1
                return stage1_result

        fake_module = types.ModuleType(
            "services.chatgpt_core.access_token_only_registration_engine"
        )
        fake_module.AccessTokenOnlyRegistrationEngine = FakeStage1Engine
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        context = ChatGPTRegistrationContext(
            email_service=mock.Mock(),
            proxy_url="",
            callback_logger=lambda _msg, *_: None,
            email=None,
            password="pw",
            browser_mode="protocol",
            max_retries=1,
            extra_config={"chatgpt_registration_mode": "refresh_token"},
        )

        with mock.patch.dict(
            "sys.modules",
            {"services.chatgpt_core.access_token_only_registration_engine": fake_module},
        ):
            result = adapter.run(context)

        self.assertTrue(result.success)
        self.assertEqual(result.access_token, "at-stage1")
        self.assertEqual(result.refresh_token, "")
        self.assertEqual(result.source, "registration_session")
        self.assertEqual(result.metadata["registration_stage"], "access_token_saved")
        self.assertEqual(result.metadata["registration_auth_capture"], "not_requested")
        account = adapter.build_account(result, "pw")
        self.assertEqual(account.extra["registration_auth_capture"], "not_requested")

    def test_browser_pending_finalizes_original_mailbox_without_replaying_signup(self):
        stage1_result = _result(
            success=True,
            email="pending@example.com",
            password="pending-pw",
            account_id="",
            workspace_id="",
            access_token="",
            refresh_token="",
            session_token="",
            source="registered_auth_pending",
            metadata={
                "registered_auth_pending": True,
                "needs_auth_capture": True,
                "registration_full_auth_failed": True,
                "registration_full_auth_error": "browser auth capture failed",
            },
        )

        class FakeStage1Engine:
            def __init__(self, **_kwargs):
                self.email = None
                self.password = None

            def run(self):
                return stage1_result

        fake_module = types.ModuleType(
            "services.chatgpt_core.access_token_only_registration_engine"
        )
        fake_module.AccessTokenOnlyRegistrationEngine = FakeStage1Engine
        email_service = mock.Mock()
        email_service.export_state.return_value = {
            "provider": "icloud_hme",
            "email": "pending@example.com",
            "status": "ready",
        }
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        context = ChatGPTRegistrationContext(
            email_service=email_service,
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda _msg, *_: None,
            email=None,
            password="pending-pw",
            browser_mode="headless",
            max_retries=1,
            extra_config={
                "chatgpt_registration_mode": "refresh_token",
                "_current_task_id": "task-pending",
            },
        )

        with mock.patch.dict(
            "sys.modules",
            {"services.chatgpt_core.access_token_only_registration_engine": fake_module},
        ), mock.patch(
            "core.db.save_account",
            side_effect=AssertionError("pending result must be saved by the outer task only"),
        ):
            result = adapter.run(context)

        self.assertIs(result, stage1_result)
        self.assertTrue(result.success)
        self.assertEqual(result.metadata["registration_stage"], "registered_auth_pending")
        self.assertEqual(result.metadata["registration_auth_capture"], "not_requested")
        self.assertNotIn("auth_capture_stage", result.metadata)
        self.assertNotIn("registration_full_auth_failed", result.metadata)
        email_service.finalize_success.assert_not_called()

    @unittest.skip("legacy two-stage registration is intentionally removed")
    def test_refresh_token_two_stage_upgrades_the_same_email(self):
        stage1_result = _result(
            email="stage1@example.com",
            password="pw-stage1",
            account_id="acct-stage1",
            workspace_id="ws-personal",
            access_token="at-stage1",
            session_token="session-stage1",
            metadata={
                "cookies": "oai-did=device; session=stage1",
                "cookie_header": "oai-did=device; session=stage1",
            },
            logs=["stage1-ok"],
        )

        class FakeStage1Engine:
            def __init__(self, **_kwargs):
                self.email = None
                self.password = None
                self._last_chatgpt_client = types.SimpleNamespace(
                    device_id="device-stage1",
                    ua="UA-stage1",
                    sec_ch_ua='"Chromium";v="136"',
                    impersonate="chrome136",
                    fingerprint={"device_id": "device-stage1"},
                )

            def run(self):
                return stage1_result

        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        context = ChatGPTRegistrationContext(
            email_service=mock.Mock(),
            proxy_url="http://127.0.0.1:7890",
            callback_logger=lambda _msg, *_: None,
            email=None,
            password="pw",
            browser_mode="protocol",
            max_retries=1,
            extra_config={"chatgpt_registration_mode": "refresh_token"},
        )
        fake_module = types.ModuleType(
            "services.chatgpt_core.access_token_only_registration_engine"
        )
        fake_module.AccessTokenOnlyRegistrationEngine = FakeStage1Engine
        captured = {}

        class FakeRegistrationResult:
            def __init__(self, **kwargs):
                values = vars(_result()).copy()
                values.update(
                    {
                        "success": False,
                        "email": "",
                        "password": "",
                        "access_token": "",
                        "refresh_token": "",
                        "session_token": "",
                        "metadata": {},
                        "logs": [],
                    }
                )
                values.update(kwargs)
                self.__dict__.update(values)

        class FakeStage2Engine:
            def __init__(self, **kwargs):
                captured["engine_kwargs"] = kwargs
                self.email_service = kwargs["email_service"]
                self.email = None
                self.password = None
                self.logs = []

            def _log(self, message, _level="info"):
                self.logs.append(str(message))

            def _capture_auth_via_fresh_login(self, **kwargs):
                captured["auth_kwargs"] = kwargs
                return {
                    "account_id": "acct-stage1",
                    "workspace_id": "ws-personal",
                    "access_token": "at-upgraded",
                    "refresh_token": "rt-upgraded",
                    "id_token": "id-upgraded",
                    "session_token": "",
                    "source": "registration_stage2_full_auth",
                }

            @staticmethod
            def _auth_payload_has_refresh_token(payload):
                return bool(payload.get("refresh_token"))

            @staticmethod
            def _apply_auth_payload_to_result(result, payload):
                result.success = True
                for key, value in payload.items():
                    setattr(result, key, value)

            @staticmethod
            def _append_gopay_provider_link_metadata(_result, _session_result):
                return None

        refresh_module = types.ModuleType(
            "services.chatgpt_core.refresh_token_registration_engine"
        )
        refresh_module.RefreshTokenRegistrationEngine = FakeStage2Engine
        refresh_module.RegistrationResult = FakeRegistrationResult
        refresh_module.EmailServiceAdapter = (
            lambda email_service, email, log_fn: types.SimpleNamespace(
                email_service=email_service,
                email=email,
                log_fn=log_fn,
            )
        )
        saved_accounts = []

        def fake_save_account(account):
            saved_accounts.append(account)
            return types.SimpleNamespace(id=42)

        with mock.patch.dict(
            "sys.modules",
            {
                "services.chatgpt_core.access_token_only_registration_engine": fake_module,
                "services.chatgpt_core.refresh_token_registration_engine": refresh_module,
            },
        ), mock.patch(
            "core.db.save_account",
            side_effect=fake_save_account,
        ):
            result = adapter.run(context)

        self.assertTrue(result.success)
        self.assertEqual(result.account_id, "acct-stage1")
        self.assertEqual(result.workspace_id, "ws-personal")
        self.assertEqual(result.refresh_token, "rt-upgraded")
        self.assertEqual(result.session_token, "session-stage1")
        self.assertEqual(result.source, "registration_stage2_full_auth")
        self.assertEqual(captured["auth_kwargs"]["email"], "stage1@example.com")
        self.assertEqual(captured["auth_kwargs"]["password"], "pw-stage1")
        self.assertEqual(captured["auth_kwargs"]["device_id"], "device-stage1")
        self.assertEqual(
            captured["auth_kwargs"]["login_source"],
            "registration_stage2_full_auth",
        )
        self.assertEqual(len(saved_accounts), 2)
        self.assertEqual(
            [account.email for account in saved_accounts],
            ["stage1@example.com", "stage1@example.com"],
        )
        self.assertEqual(saved_accounts[-1].user_id, "acct-stage1")
        self.assertEqual(saved_accounts[-1].extra["workspace_id"], "ws-personal")
        self.assertEqual(saved_accounts[-1].extra["refresh_token"], "rt-upgraded")
        self.assertEqual(saved_accounts[-1].extra["session_token"], "session-stage1")
        self.assertEqual(
            saved_accounts[-1].extra["cookies"],
            "oai-did=device; session=stage1",
        )
        self.assertEqual(
            saved_accounts[-1].extra["cookie_header"],
            "oai-did=device; session=stage1",
        )
        self.assertEqual(saved_accounts[-1].status.value, "registered")
        self.assertNotIn("chatgpt_workspace_variant_key", saved_accounts[-1].extra)
        self.assertTrue(
            saved_accounts[-1].extra["registration_web_session_material_preserved"]
        )


if __name__ == "__main__":
    unittest.main()
