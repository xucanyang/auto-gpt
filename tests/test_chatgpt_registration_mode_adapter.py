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
    def test_resolve_defaults_to_refresh_token_mode(self):
        self.assertEqual(
            resolve_chatgpt_registration_mode({}),
            CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
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

    def test_refresh_token_two_stage_keeps_saved_access_token_when_upgrade_fails(self):
        stage1_result = _result(
            email="stage1@example.com",
            password="pw-stage1",
            account_id="acct-stage1",
            workspace_id="ws-personal",
            access_token="at-stage1",
            session_token="session-stage1",
            metadata={"mailbox_state": {"provider": "icloud_hme"}},
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
            extra_config={
                "chatgpt_registration_mode": "refresh_token",
                "_current_task_id": "task-1",
            },
        )
        fake_module = types.ModuleType(
            "services.chatgpt_core.access_token_only_registration_engine"
        )
        fake_module.AccessTokenOnlyRegistrationEngine = FakeStage1Engine
        failed_upgrade = _result(
            success=False,
            email="stage1@example.com",
            access_token="",
            error_message="full auth failed",
            logs=["stage2-failed"],
        )

        with mock.patch.dict(
            "sys.modules",
            {"services.chatgpt_core.access_token_only_registration_engine": fake_module},
        ), mock.patch("core.db.save_account", return_value=types.SimpleNamespace(id=42)) as save_account, mock.patch.object(
            adapter,
            "_capture_stage2_from_stage1_session",
            return_value=failed_upgrade,
        ):
            result = adapter.run(context)

        self.assertTrue(result.success)
        self.assertEqual(result.access_token, "at-stage1")
        self.assertEqual(result.refresh_token, "")
        save_account.assert_called_once()
        saved_account = save_account.call_args.args[0]
        self.assertEqual(saved_account.email, "stage1@example.com")
        self.assertEqual(saved_account.user_id, "acct-stage1")
        self.assertEqual(saved_account.extra["workspace_id"], "ws-personal")
        self.assertEqual(saved_account.extra["auth_level"], "access_token_only")
        context.email_service.finalize_success.assert_called_once()

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
