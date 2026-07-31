import re
from unittest import mock

import pytest

from api import config as config_api


RETIRED_CONFIG_KEYS = {
    "chatgpt_gopay_defaults",
    "chatgpt_gopay_otp_auto_resend_delay_seconds",
    "chatgpt_gopay_phone_candidates",
    "chatgpt_gopay_uid_bindings",
    "chatgpt_gopay_uid_sessions",
    "chatgpt_gopay_smsforwarder_secret",
    "chatgpt_gopay_smsforwarder_recent_events",
}

RETIRED_OPENAPI_SEGMENTS = (
    "/api/pipeline",
    "/api/idea-oaipay-pipeline",
    "/api/integrations/gopay-otp",
    "/api/chatgpt/gopay/",
)


def test_openapi_does_not_expose_retired_payment_and_pipeline_routes():
    from main import app

    paths = set(app.openapi().get("paths") or {})
    assert not any(path.startswith("/api/pipeline") for path in paths)
    assert not any(path.startswith("/api/idea-oaipay-pipeline") for path in paths)
    assert not any("/integrations/gopay-otp" in path for path in paths)
    assert not any("/gopay/" in path for path in paths)


def test_config_response_and_update_allowlist_drop_retired_keys():
    assert RETIRED_CONFIG_KEYS.isdisjoint(config_api.CONFIG_KEYS)
    stored = {key: "legacy-value" for key in RETIRED_CONFIG_KEYS}

    with mock.patch.object(config_api.config_store, "get_all", return_value=dict(stored)):
        response = config_api._build_config_response()

    assert RETIRED_CONFIG_KEYS.isdisjoint(response)

    with mock.patch.object(config_api.config_store, "get_all", return_value={}), mock.patch.object(
        config_api.config_store,
        "set_many",
    ) as set_many:
        result = config_api.update_config(
            config_api.ConfigUpdate(data=stored)
        )

    assert result == {"ok": True, "updated": []}
    set_many.assert_called_once_with({}, base_revision=None)


@pytest.mark.parametrize("path", RETIRED_OPENAPI_SEGMENTS)
def test_retired_openapi_segments_are_absent_from_schemas(path):
    from main import app

    paths = set(app.openapi().get("paths") or {})
    assert not any(candidate.startswith(path) for candidate in paths)
