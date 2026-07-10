import unittest
from unittest.mock import patch

from api.tasks import RegisterTaskRequest, _prepare_register_request


class TempMailRegisterRequestTests(unittest.TestCase):
    def _prepare_with_config(self, request: RegisterTaskRequest, config: dict):
        def get_config_value(key: str, default: str = ""):
            return config.get(key, default)

        with (
            patch("core.config_store.config_store.get", side_effect=get_config_value),
            patch("core.config_store.config_store.get_all", return_value=dict(config)),
        ):
            return _prepare_register_request(request)

    def test_fixed_domain_falls_back_to_global_domain_list_when_request_omits_it(self):
        prepared = self._prepare_with_config(
            RegisterTaskRequest(
                platform="chatgpt",
                extra={"mail_provider": "tempmail_api"},
            ),
            {
                "tempmail_mode": "fixed_domain",
                "tempmail_fixed_domains": "@global-one.example\nglobal-two.example",
                "tempmail_primary_domain": "",
            },
        )

        self.assertEqual(prepared.extra["tempmail_mode"], "fixed_domain")
        self.assertEqual(
            prepared.extra["tempmail_fixed_domains"],
            ["global-one.example", "global-two.example"],
        )
        self.assertEqual(prepared.extra["tempmail_primary_domain"], "global-one.example")

    def test_fixed_domain_request_list_takes_precedence_and_keeps_all_domains(self):
        prepared = self._prepare_with_config(
            RegisterTaskRequest(
                platform="chatgpt",
                extra={
                    "mail_provider": "tempmail_local",
                    "tempmail_mode": "fixed_domain",
                    "tempmail_primary_domain": "request-one.example",
                    "tempmail_fixed_domains": [
                        "@request-one.example",
                        "request-two.example",
                    ],
                },
            ),
            {
                "tempmail_mode": "fixed_domain",
                "tempmail_fixed_domains": "global-one.example,global-two.example",
                "tempmail_primary_domain": "global-one.example",
            },
        )

        self.assertEqual(
            prepared.extra["tempmail_fixed_domains"],
            ["request-one.example", "request-two.example"],
        )
        self.assertEqual(prepared.extra["tempmail_primary_domain"], "request-one.example")

    def test_task_subdomain_does_not_require_fixed_domains(self):
        prepared = self._prepare_with_config(
            RegisterTaskRequest(
                platform="chatgpt",
                extra={"mail_provider": "tempmail_api"},
            ),
            {
                "tempmail_mode": "task_subdomain",
                "tempmail_fixed_domains": "",
                "tempmail_primary_domain": "",
            },
        )

        self.assertEqual(prepared.extra["mail_provider"], "tempmail_api")

