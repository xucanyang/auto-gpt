from unittest import mock

from api import config as config_api
from core.base_mailbox import HmeReadyApiClient, TempMailLocalMailbox


def test_legacy_tempmail_endpoints_resolve_to_the_internal_api():
    expected = "http://tempmail-api-1:8080"

    assert TempMailLocalMailbox._normalize_api_url("https://tempmail.cccy.me/") == expected
    assert TempMailLocalMailbox._normalize_api_url("http://127.0.0.1:18083") == expected
    assert TempMailLocalMailbox._normalize_api_url("http://localhost:18081") == expected
    assert TempMailLocalMailbox._normalize_api_url("https://mail.example.test") == "https://mail.example.test"


def test_legacy_hme_helper_endpoints_resolve_to_the_internal_control_plane():
    expected = "http://172.20.0.1:18765"

    assert HmeReadyApiClient(api_url="https://hme.cccy.me/").api_url == expected
    assert HmeReadyApiClient(api_url="http://host.docker.internal:18765").api_url == expected
    assert HmeReadyApiClient(api_url="https://helper.example.test").api_url == "https://helper.example.test"


def test_config_update_persists_canonical_mailbox_endpoints():
    saved = {}

    with (
        mock.patch.object(config_api.config_store, "get_all", return_value={}),
        mock.patch.object(
            config_api.config_store,
            "set_many",
            side_effect=lambda values, **_kwargs: saved.update(values),
        ),
    ):
        result = config_api.update_config(
            config_api.ConfigUpdate(
                data={
                    "tempmail_api_url": "https://tempmail.cccy.me",
                    "icloud_hme_helper_api_url": "https://hme.cccy.me",
                }
            )
        )

    assert result["ok"] is True
    assert saved["tempmail_api_url"] == "http://tempmail-api-1:8080"
    assert saved["icloud_hme_helper_api_url"] == "http://172.20.0.1:18765"
