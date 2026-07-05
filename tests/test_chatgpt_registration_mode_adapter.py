import unittest
import types
from unittest import mock

from services.chatgpt_core.chatgpt_registration_mode_adapter import (
    CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
    resolve_chatgpt_registration_mode,
)


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

    def test_build_account_marks_selected_mode(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "demo@example.com",
                "password": "pw",
                "account_id": "acct-demo",
                "access_token": "at-demo",
                "refresh_token": "",
                "id_token": "id-demo",
                "session_token": "session-demo",
                "workspace_id": "ws-demo",
                "source": "register",
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertEqual(account.email, "demo@example.com")
        self.assertEqual(account.password, "pw")
        self.assertEqual(
            account.extra["chatgpt_registration_mode"],
            CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
        )
        self.assertFalse(account.extra["chatgpt_has_refresh_token_solution"])

    def test_build_account_propagates_checkout_skip_save_metadata(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "demo@example.com",
                "password": "pw",
                "account_id": "acct-demo",
                "access_token": "at-demo",
                "refresh_token": "",
                "id_token": "",
                "session_token": "session-demo",
                "workspace_id": "ws-demo",
                "source": "register",
                "metadata": {
                    "chatgpt_checkout_url": "https://chatgpt.com/checkout/openai_llc/cs_live_123",
                    "chatgpt_checkout_amount": "34900000",
                    "chatgpt_checkout_amount_is_zero": False,
                    "chatgpt_skip_save_account": True,
                    "chatgpt_skip_save_reason": "Plus checkout amount != 0",
                },
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

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
        result = type(
            "Result",
            (),
            {
                "email": "demo@example.com",
                "password": "pw",
                "account_id": "acct-demo",
                "access_token": "at-demo",
                "refresh_token": "",
                "id_token": "",
                "session_token": "session-demo",
                "workspace_id": "ws-demo",
                "source": "register",
                "metadata": {
                    "chatgpt_gopay_provider_link_enabled": True,
                    "chatgpt_gopay_provider_link_ready": True,
                    "chatgpt_gopay_provider_link": "https://app.midtrans.com/snap/v4/redirection/11111111-1111-1111-1111-111111111111",
                    "chatgpt_gopay_provider_link_cs_id": "cs_live_123",
                    "chatgpt_gopay_provider_link_payment_method_types": ["gopay"],
                },
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertTrue(account.extra["chatgpt_gopay_provider_link_enabled"])
        self.assertTrue(account.extra["chatgpt_gopay_provider_link_ready"])
        self.assertEqual(
            account.extra["chatgpt_gopay_provider_link"],
            "https://app.midtrans.com/snap/v4/redirection/11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual(account.extra["chatgpt_gopay_provider_link_payment_method_types"], ["gopay"])

    def test_build_account_marks_already_paid_checkout_as_invalid(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "paid@example.com",
                "password": "pw",
                "account_id": "acct-demo",
                "access_token": "at-demo",
                "refresh_token": "",
                "id_token": "",
                "session_token": "session-demo",
                "workspace_id": "ws-demo",
                "source": "register",
                "metadata": {
                    "chatgpt_checkout_error_code": "already_paid",
                    "chatgpt_account_unavailable": True,
                    "chatgpt_payment_already_paid": True,
                    "chatgpt_skip_save_account": True,
                    "chatgpt_skip_save_reason": "Plus checkout 已付费响应: you have paid",
                },
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertEqual(account.status.value, "invalid")
        self.assertTrue(account.extra["chatgpt_payment_already_paid"])
        self.assertTrue(account.extra["chatgpt_skip_save_account"])

    def test_build_account_marks_registration_access_token_partial_auth(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "demo@example.com",
                "password": "pw",
                "account_id": "acct-demo",
                "access_token": "at-demo",
                "refresh_token": "",
                "id_token": "",
                "session_token": "session-demo",
                "workspace_id": "acct-demo",
                "source": "registration_session",
                "metadata": {"registration_access_token_saved": True},
                "workspace_artifacts": [
                    {
                        "scope": "free",
                        "label": "registration_at",
                        "account_id": "acct-demo",
                        "workspace_id": "acct-demo",
                        "access_token": "at-demo",
                        "session_token": "session-demo",
                        "source": "registration_session",
                        "variant_key": "registration_at:acct-demo",
                        "auth_level": "access_token_only",
                        "partial_auth": True,
                    }
                ],
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertEqual(account.token, "at-demo")
        self.assertEqual(account.status.value, "pending_payment")
        self.assertEqual(account.extra["chatgpt_token_source"], "registration_session")

    def test_build_account_saves_k12_workspace_as_linked_variant(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "demo@example.com",
                "password": "pw",
                "account_id": "acct-free",
                "access_token": "at-free",
                "refresh_token": "",
                "id_token": "",
                "session_token": "session-free",
                "workspace_id": "acct-free",
                "source": "register",
                "metadata": {
                    "chatgpt_k12_join_summary": {"enabled": True, "saved_spaces": 2},
                    "cookies": "base-cookie=1",
                    "cookie_header": "base-cookie=1",
                },
                "workspace_artifacts": [
                    {
                        "scope": "free",
                        "label": "free",
                        "account_id": "acct-free",
                        "workspace_id": "acct-free",
                        "access_token": "at-free",
                        "session_token": "session-free",
                        "source": "registration_session",
                        "variant_key": "free:acct-free",
                        "auth_level": "access_token_only",
                        "partial_auth": True,
                    },
                    {
                        "scope": "k12",
                        "label": "k12",
                        "account_id": "ws-k12",
                        "workspace_id": "ws-k12",
                        "access_token": "at-k12",
                        "session_token": "session-free",
                        "cookies": "cookie=1",
                        "source": "k12_workspace_join",
                        "variant_key": "k12:ws-k12",
                        "auth_level": "access_token_only",
                        "partial_auth": True,
                        "space": {"name": "School Lab", "structure": "workspace", "plan_type": "edu"},
                    },
                ],
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertEqual(account.extra["chatgpt_workspace_variant_key"], "free:acct-free")
        linked = account.extra["_linked_accounts_to_save"]
        self.assertEqual(len(linked), 1)
        linked_extra = linked[0]["extra"]
        self.assertEqual(linked_extra["chatgpt_workspace_scope"], "k12")
        self.assertEqual(linked_extra["chatgpt_workspace_variant_key"], "k12:ws-k12")
        self.assertEqual(linked_extra["chatgpt_workspace_display_name"], "School Lab")
        self.assertEqual(linked_extra["chatgpt_token_source"], "k12_workspace_join")
        self.assertEqual(linked_extra["cookies"], "cookie=1")
        self.assertEqual(account.extra["chatgpt_workspace_variants"][1]["scope"], "k12")

    def test_build_account_carries_full_auth_failure_and_phone_challenge_metadata(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        result = type(
            "Result",
            (),
            {
                "email": "demo@example.com",
                "password": "pw",
                "account_id": "acct-demo",
                "access_token": "at-demo",
                "refresh_token": "",
                "id_token": "",
                "session_token": "session-demo",
                "workspace_id": "acct-demo",
                "source": "registration_session",
                "metadata": {
                    "registration_access_token_saved": True,
                    "registration_full_auth_failed": True,
                    "registration_full_auth_error": "add_phone required",
                    "needs_auth_capture": True,
                    "chatgpt_phone_challenge": {
                        "type": "add_phone",
                        "status": "unbound_required",
                        "display": "未绑定手机号",
                    },
                },
                "workspace_artifacts": [
                    {
                        "scope": "free",
                        "label": "registration_at",
                        "account_id": "acct-demo",
                        "workspace_id": "acct-demo",
                        "access_token": "at-demo",
                        "session_token": "session-demo",
                        "source": "registration_session",
                        "variant_key": "registration_at:acct-demo",
                        "auth_level": "access_token_only",
                        "partial_auth": True,
                    }
                ],
            },
        )()

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertEqual(account.status.value, "pending_payment")
        self.assertTrue(account.extra["needs_auth_capture"])
        self.assertTrue(account.extra["auth_capture_required"])
        self.assertTrue(account.extra["registration_full_auth_failed"])
        self.assertEqual(account.extra["registration_full_auth_error"], "add_phone required")
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
                return type("Result", (), {"success": True})()

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

        fake_module = types.ModuleType("services.chatgpt_core.access_token_only_registration_engine")
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

    def test_refresh_token_adapter_two_stage_saves_at_before_full_auth_failure(self):
        created = {"stage1": {}}
        email_service = mock.Mock()
        email_service.create_email.return_value = {"email": "stage1@example.com"}

        class FakeStage1Engine:
            def __init__(self, **kwargs):
                created["stage1"]["kwargs"] = kwargs
                self.email = None
                self.password = None

            def run(self):
                created["stage1"]["email"] = self.email
                return types.SimpleNamespace(
                    success=True,
                    email="stage1@example.com",
                    password="pw-stage1",
                    account_id="acct-stage1",
                    workspace_id="ws-stage1",
                    access_token="at-stage1",
                    refresh_token="",
                    id_token="",
                    session_token="session-stage1",
                    source="register",
                    metadata={"mailbox_state": {"provider": "icloud_hme"}},
                    logs=["stage1-ok"],
                    workspace_artifacts=None,
                    error_message="",
                )

        fake_stage1_module = types.ModuleType("services.chatgpt_core.access_token_only_registration_engine")
        fake_stage1_module.AccessTokenOnlyRegistrationEngine = FakeStage1Engine

        saved_accounts = []

        def fake_save_account(account):
            saved_accounts.append(account)
            return types.SimpleNamespace(id=42)

        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "refresh_token"}
        )
        context = ChatGPTRegistrationContext(
            email_service=email_service,
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

        with mock.patch.dict(
            "sys.modules",
            {
                "services.chatgpt_core.access_token_only_registration_engine": fake_stage1_module,
            },
        ), mock.patch("core.db.save_account", side_effect=fake_save_account):
            result = adapter.run(context)

        self.assertTrue(result.success)
        self.assertEqual(result.access_token, "at-stage1")
        self.assertEqual(result.refresh_token, "")
        self.assertEqual(created["stage1"]["kwargs"]["extra_config"]["chatgpt_registration_mode"], "access_token_only")
        self.assertFalse(created["stage1"]["kwargs"]["extra_config"]["chatgpt_has_refresh_token_solution"])
        self.assertFalse(created["stage1"]["kwargs"]["extra_config"]["chatgpt_existing_account_capture"])
        self.assertFalse(created["stage1"]["kwargs"]["extra_config"]["chatgpt_access_token_only_checkout_amount_check_enabled"])
        self.assertEqual(len(saved_accounts), 1)
        self.assertEqual(saved_accounts[0].extra["chatgpt_workspace_variant_key"], "free:acct-stage1")
        self.assertEqual(saved_accounts[0].extra["auth_level"], "access_token_only")
        self.assertTrue(saved_accounts[0].extra["partial_auth"])
        self.assertNotIn("registration_full_auth_failed", saved_accounts[0].extra)
        self.assertNotIn("needs_auth_capture", saved_accounts[0].extra)
        self.assertNotIn("auth_capture_required", saved_accounts[0].extra)
        email_service.finalize_success.assert_called_once()
        email_service.finalize_failure.assert_not_called()

    def test_refresh_token_adapter_two_stage_upgrades_same_variant_on_full_auth_success(self):
        created = {"stage2": {}}

        class FakeStage1Engine:
            def __init__(self, **kwargs):
                self.email = None
                self.password = None
                self._last_chatgpt_client = types.SimpleNamespace(
                    session=object(),
                    device_id="device-stage1",
                    ua="UA",
                    sec_ch_ua='"Chromium";v="136"',
                    impersonate="chrome136",
                    accept_language="en-US",
                    fingerprint={"device_id": "device-stage1"},
                )

            def run(self):
                return types.SimpleNamespace(
                    success=True,
                    email="stage1@example.com",
                    password="pw-stage1",
                    account_id="acct-stage1",
                    workspace_id="ws-stage1",
                    access_token="at-stage1",
                    refresh_token="",
                    id_token="",
                    session_token="session-stage1",
                    source="register",
                    metadata={"cookies": "oai-did=device; __Secure-next-auth.session-token=session-stage1"},
                    logs=["stage1-ok"],
                    workspace_artifacts=None,
                    error_message="",
                )

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
                    "error_message": "",
                    "logs": [],
                    "metadata": {},
                    "source": "register",
                    "workspace_artifacts": None,
                }
                defaults.update(kwargs)
                self.__dict__.update(defaults)

        class FakeStage2Engine:
            def __init__(self, **kwargs):
                created["stage2"]["kwargs"] = kwargs
                self.email = None
                self.password = None
                self.logs = []
                self._last_workspace_capture_error = ""
                self.email_service = kwargs.get("email_service")

            def _log(self, message, level="info"):
                self.logs.append(str(message))

            def _build_oauth_client(self):
                created["stage2"]["build_oauth_client"] = True
                return types.SimpleNamespace(last_error="")

            def _reuse_register_browser_context(self, register_client, oauth_client):
                created["stage2"]["reused_session"] = register_client.session
                created["stage2"]["device_id"] = register_client.device_id

            def _capture_workspace_artifact_via_fresh_login(self, **kwargs):
                created["stage2"]["capture_kwargs"] = kwargs
                return {
                    "scope": "free",
                    "label": "free",
                    "account_id": "acct-stage2",
                    "workspace_id": "ws-stage2",
                    "access_token": "at-stage2",
                    "refresh_token": "rt-stage2",
                    "id_token": "id-stage2",
                    "session_token": "",
                    "source": "workspace_capture_free",
                    "variant_key": "free:ws-stage2",
                }

            def _artifact_has_refresh_token(self, artifact):
                return bool(artifact.get("refresh_token"))

            def _apply_workspace_artifact_to_result(self, result, artifact):
                result.success = True
                result.email = self.email or result.email
                result.password = self.password or result.password
                result.account_id = artifact["account_id"]
                result.workspace_id = artifact["workspace_id"]
                result.access_token = artifact["access_token"]
                result.refresh_token = artifact["refresh_token"]
                result.id_token = artifact["id_token"]
                result.session_token = artifact["session_token"]
                result.source = artifact["source"]

            def _append_gopay_provider_link_metadata(self, result, session_result=None):
                return None

        fake_stage1_module = types.ModuleType("services.chatgpt_core.access_token_only_registration_engine")
        fake_stage1_module.AccessTokenOnlyRegistrationEngine = FakeStage1Engine
        fake_stage2_module = types.ModuleType("services.chatgpt_core.refresh_token_registration_engine")
        fake_stage2_module.RefreshTokenRegistrationEngine = FakeStage2Engine
        fake_stage2_module.RegistrationResult = FakeRegistrationResult
        fake_stage2_module.EmailServiceAdapter = lambda email_service, email, log_fn: types.SimpleNamespace(email_service=email_service, email=email, log_fn=log_fn)
        saved_accounts = []

        def fake_save_account(account):
            saved_accounts.append(account)
            return types.SimpleNamespace(id=42)

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

        with mock.patch.dict(
            "sys.modules",
            {
                "services.chatgpt_core.access_token_only_registration_engine": fake_stage1_module,
                "services.chatgpt_core.refresh_token_registration_engine": fake_stage2_module,
            },
        ), mock.patch("core.db.save_account", side_effect=fake_save_account):
            result = adapter.run(context)

        self.assertTrue(result.success)
        self.assertEqual(result.refresh_token, "rt-stage2")
        self.assertEqual(result.metadata["auth_capture_stage"], "success")
        self.assertEqual(result.metadata["auth_capture_method"], "registration_stage2_full_auth")
        self.assertEqual(len(saved_accounts), 2)
        self.assertEqual(saved_accounts[0].extra["chatgpt_workspace_variant_key"], "free:acct-stage1")
        self.assertEqual(saved_accounts[1].extra["chatgpt_workspace_variant_key"], "free:acct-stage1")
        self.assertEqual(saved_accounts[1].extra["refresh_token"], "rt-stage2")
        self.assertEqual(saved_accounts[1].extra["session_token"], "session-stage1")
        self.assertEqual(saved_accounts[1].extra["cookies"], "oai-did=device; __Secure-next-auth.session-token=session-stage1")
        self.assertTrue(saved_accounts[1].extra["registration_web_session_material_preserved"])
        self.assertEqual(result.session_token, "session-stage1")
        self.assertEqual(saved_accounts[1].status.value, "registered")
        self.assertEqual(created["stage2"]["capture_kwargs"]["scope"], "free")
        self.assertEqual(created["stage2"]["capture_kwargs"]["device_id"], "device-stage1")
        self.assertEqual(created["stage2"]["capture_kwargs"]["login_source"], "registration_stage2_full_auth")

    def test_refresh_token_two_stage_keeps_stage1_k12_artifact_after_free_rt_upgrade(self):
        class FakeStage1Engine:
            def __init__(self, **kwargs):
                self.email = None
                self.password = None
                self._last_chatgpt_client = types.SimpleNamespace(
                    session=object(),
                    device_id="device-stage1",
                    ua="UA",
                    sec_ch_ua='"Chromium";v="136"',
                    impersonate="chrome136",
                    fingerprint={"device_id": "device-stage1"},
                )

            def run(self):
                return types.SimpleNamespace(
                    success=True,
                    email="stage1@example.com",
                    password="pw-stage1",
                    account_id="acct-stage1",
                    workspace_id="acct-stage1",
                    access_token="at-stage1",
                    refresh_token="",
                    id_token="",
                    session_token="session-stage1",
                    source="register",
                    metadata={"cookies": "cookie=1", "chatgpt_k12_join_summary": {"enabled": True}},
                    logs=["stage1-ok"],
                    workspace_artifacts=[
                        {
                            "scope": "free",
                            "label": "free",
                            "account_id": "acct-stage1",
                            "workspace_id": "acct-stage1",
                            "access_token": "at-stage1",
                            "session_token": "session-stage1",
                            "source": "registration_session",
                            "variant_key": "free:acct-stage1",
                            "auth_level": "access_token_only",
                            "partial_auth": True,
                        },
                        {
                            "scope": "k12",
                            "label": "k12",
                            "account_id": "ws-k12",
                            "workspace_id": "ws-k12",
                            "access_token": "at-k12",
                            "session_token": "session-stage1",
                            "cookies": "cookie=1",
                            "source": "k12_workspace_join",
                            "variant_key": "k12:ws-k12",
                            "auth_level": "access_token_only",
                            "partial_auth": True,
                        },
                    ],
                    error_message="",
                )

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
                    "error_message": "",
                    "logs": [],
                    "metadata": {},
                    "source": "register",
                    "workspace_artifacts": None,
                }
                defaults.update(kwargs)
                self.__dict__.update(defaults)

        class FakeStage2Engine:
            def __init__(self, **kwargs):
                self.email = None
                self.password = None
                self.logs = []
                self._last_workspace_capture_error = ""
                self.email_service = kwargs.get("email_service")

            def _log(self, message, level="info"):
                self.logs.append(str(message))

            def _capture_workspace_artifact_via_fresh_login(self, **kwargs):
                return {
                    "scope": "free",
                    "label": "free",
                    "account_id": "acct-stage2",
                    "workspace_id": "acct-stage2",
                    "access_token": "at-stage2",
                    "refresh_token": "rt-stage2",
                    "id_token": "id-stage2",
                    "session_token": "",
                    "source": "workspace_capture_free",
                    "variant_key": "free:acct-stage2",
                }

            def _artifact_has_refresh_token(self, artifact):
                return bool(artifact.get("refresh_token"))

            def _apply_workspace_artifact_to_result(self, result, artifact):
                result.success = True
                result.email = self.email or result.email
                result.password = self.password or result.password
                result.account_id = artifact["account_id"]
                result.workspace_id = artifact["workspace_id"]
                result.access_token = artifact["access_token"]
                result.refresh_token = artifact["refresh_token"]
                result.id_token = artifact["id_token"]
                result.session_token = artifact["session_token"]
                result.source = artifact["source"]

            def _append_gopay_provider_link_metadata(self, result, session_result=None):
                return None

        fake_stage1_module = types.ModuleType("services.chatgpt_core.access_token_only_registration_engine")
        fake_stage1_module.AccessTokenOnlyRegistrationEngine = FakeStage1Engine
        fake_stage2_module = types.ModuleType("services.chatgpt_core.refresh_token_registration_engine")
        fake_stage2_module.RefreshTokenRegistrationEngine = FakeStage2Engine
        fake_stage2_module.RegistrationResult = FakeRegistrationResult
        fake_stage2_module.EmailServiceAdapter = lambda email_service, email, log_fn: types.SimpleNamespace(email_service=email_service, email=email, log_fn=log_fn)
        saved_accounts = []

        def fake_save_account(account):
            saved_accounts.append(account)
            return types.SimpleNamespace(id=42)

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

        with mock.patch.dict(
            "sys.modules",
            {
                "services.chatgpt_core.access_token_only_registration_engine": fake_stage1_module,
                "services.chatgpt_core.refresh_token_registration_engine": fake_stage2_module,
            },
        ), mock.patch("core.db.save_account", side_effect=fake_save_account):
            result = adapter.run(context)

        self.assertTrue(result.success)
        self.assertEqual([item["scope"] for item in result.workspace_artifacts], ["free", "k12"])
        final_account = adapter.build_account(result, fallback_password="fallback")
        self.assertEqual(final_account.extra["_linked_accounts_to_save"][0]["extra"]["chatgpt_workspace_scope"], "k12")
        self.assertEqual(final_account.extra["_linked_accounts_to_save"][0]["extra"]["access_token"], "at-k12")


if __name__ == "__main__":
    unittest.main()
