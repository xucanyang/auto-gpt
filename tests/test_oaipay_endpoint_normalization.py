from unittest import mock

from api import config as config_api
from core.db import AccountModel
from services import oaipay_sync
from services.chatgpt_core import oaipay_upload


class _Response:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def _reset_category_cache() -> None:
    oaipay_upload._CATEGORIES_CACHE = {}
    oaipay_upload._CATEGORIES_ID_TO_NAME_CACHE = {}
    oaipay_upload._CATEGORIES_ITEMS_CACHE = []
    oaipay_upload._CATEGORIES_CACHE_TIME = 0
    oaipay_upload._CATEGORIES_CACHE_KEY = ""


def test_legacy_public_oaipay_url_uses_private_service_and_keeps_custom_endpoint():
    assert (
        oaipay_upload.normalize_oaipay_api_url("https://gpt.cccy.me/api/auto-gpt/upload/")
        == "http://gpt-cccy-me:8789/api/auto-gpt/upload"
    )
    assert oaipay_upload.normalize_oaipay_api_url("http://gpt.cccy.me") == "http://gpt-cccy-me:8789"
    assert (
        oaipay_upload.normalize_oaipay_api_url("https://oaipay.example.test/api/auto-gpt/upload")
        == "https://oaipay.example.test/api/auto-gpt/upload"
    )
    assert (
        oaipay_upload.normalize_oaipay_api_url("https://gpt.cccy.me:8443/api/auto-gpt/upload")
        == "https://gpt.cccy.me:8443/api/auto-gpt/upload"
    )


def test_config_update_persists_private_oaipay_endpoint():
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
                data={"oaipay_api_url": "https://gpt.cccy.me/api/auto-gpt/upload"}
            )
        )

    assert result["ok"] is True
    assert saved["oaipay_api_url"] == "http://gpt-cccy-me:8789/api/auto-gpt/upload"


def test_category_fetch_uses_private_upload_key_endpoint_first():
    _reset_category_cache()
    with mock.patch.object(
        oaipay_upload.cffi_requests,
        "get",
        return_value=_Response(200, [{"id": 7, "name": "PLUS--未接码"}]),
    ) as get:
        categories = oaipay_upload.fetch_oaipay_categories(
            "https://gpt.cccy.me/api/auto-gpt/upload",
            "upload-key",
            force_refresh=True,
        )

    assert categories == [{"id": 7, "name": "PLUS--未接码"}]
    assert get.call_args.args[0] == "http://gpt-cccy-me:8789/api/auto-gpt/categories"


def test_oaipay_account_probe_uses_private_upload_key_endpoint_first():
    values = {
        "oaipay_api_url": "https://gpt.cccy.me/api/auto-gpt/upload",
        "oaipay_api_key": "upload-key",
        "oaipay_probe_timeout_seconds": "1",
        "oaipay_probe_api_page_size": "10",
    }
    with (
        mock.patch.object(oaipay_sync, "_get_config_value", side_effect=lambda key, default="": values.get(key, default)),
        mock.patch.object(
            oaipay_sync.cffi_requests,
            "get",
            return_value=_Response(200, {"items": []}),
        ) as get,
    ):
        assert oaipay_sync._fetch_oaipay_account_items({"email": "demo@example.test"}) == []

    assert get.call_args.args[0] == "http://gpt-cccy-me:8789/api/auto-gpt/accounts"


def test_auto_upload_uses_private_endpoint_for_legacy_public_url():
    account = AccountModel(
        platform="chatgpt",
        email="demo@example.test",
        password="password",
        status="registered",
    )
    account.set_extra(
        {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "workspace_id": "workspace-id",
        }
    )
    with mock.patch.object(
        oaipay_upload.cffi_requests,
        "post",
        return_value=_Response(200, {"success": True, "imported": 1}),
    ) as post:
        result = oaipay_upload.upload_to_oaipay_detailed(
            account,
            api_url="https://gpt.cccy.me/api/auto-gpt/upload",
            api_key="upload-key",
            group_ids=[1],
            category_mode="manual",
            capabilities={"has_refresh_token": True, "has_paid_subscription": False},
        )

    assert result["ok"] is True
    assert post.call_args.args[0] == "http://gpt-cccy-me:8789/api/auto-gpt/upload"
