from unittest import mock

import pytest
from fastapi import HTTPException

from api import config as config_api


def _config_store(initial):
    state = dict(initial)

    def set_many(values, **_kwargs):
        state.update(values)

    return state, mock.patch.object(
        config_api.config_store,
        "get_all",
        side_effect=lambda: dict(state),
    ), mock.patch.object(config_api.config_store, "set_many", side_effect=set_many)


def test_cliproxy_can_stage_miyaip_credentials_independently():
    state, get_all, set_many = _config_store(
        {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_provider": "cliproxy",
            "dynamic_proxy_template": "http://region-XX-sid-seed.proxy.test:8080",
        }
    )

    with get_all, set_many:
        first = config_api.update_config(
            config_api.ConfigUpdate(data={"miyaip_crc": "crc-sensitive-value"})
        )
        second = config_api.update_config(
            config_api.ConfigUpdate(data={"miyaip_key_name": "key-sensitive-value"})
        )

    assert first["ok"] is True
    assert second["ok"] is True
    assert state["dynamic_proxy_provider"] == "cliproxy"
    assert state["miyaip_crc"] == "crc-sensitive-value"
    assert state["miyaip_key_name"] == "key-sensitive-value"
    assert state["dynamic_proxy_template"] == "http://region-XX-sid-seed.proxy.test:8080"


def test_cliproxy_provider_colon_export_is_canonicalized_on_save():
    state, get_all, set_many = _config_store(
        {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_provider": "cliproxy",
        }
    )

    with get_all, set_many:
        result = config_api.update_config(
            config_api.ConfigUpdate(
                data={
                    "dynamic_proxy_template": (
                        "us.arxlabs.io:3010:"
                        "acct-region-Rand-sid-seed-t-5:secret-value"
                    )
                }
            )
        )

    assert result["ok"] is True
    assert state["dynamic_proxy_template"] == (
        "http://acct-region-Rand-sid-seed-t-5:secret-value@us.arxlabs.io:3010"
    )


def test_switching_to_miyaip_requires_complete_saved_credentials():
    incomplete_state, get_all, set_many = _config_store(
        {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_provider": "cliproxy",
            "miyaip_crc": "crc-sensitive-value",
        }
    )

    with get_all, set_many as set_many_mock:
        with pytest.raises(HTTPException) as error:
            config_api.update_config(
                config_api.ConfigUpdate(data={"dynamic_proxy_provider": "miyaip"})
            )

    assert error.value.status_code == 400
    assert "KeyName" in str(error.value.detail)
    set_many_mock.assert_not_called()
    assert incomplete_state["dynamic_proxy_provider"] == "cliproxy"

    complete_state, get_all, set_many = _config_store(
        {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_provider": "cliproxy",
            "dynamic_proxy_template": "http://region-XX-sid-seed.proxy.test:8080",
            "miyaip_crc": "crc-sensitive-value",
            "miyaip_key_name": "key-sensitive-value",
            "miyaip_pool": "2",
            "miyaip_gateway_server": "as",
            "miyaip_protocol": "http",
            "miyaip_request_timeout_seconds": "15",
        }
    )

    with get_all, set_many:
        result = config_api.update_config(
            config_api.ConfigUpdate(data={"dynamic_proxy_provider": "miyaip"})
        )

    assert result["ok"] is True
    assert complete_state["dynamic_proxy_provider"] == "miyaip"
    assert complete_state["dynamic_proxy_template"] == "http://region-XX-sid-seed.proxy.test:8080"
    assert complete_state["miyaip_crc"] == "crc-sensitive-value"
    assert complete_state["miyaip_key_name"] == "key-sensitive-value"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("miyaip_pool", "0", "Pool"),
        ("miyaip_gateway_server", "af", "us / as / eu"),
        ("miyaip_protocol", "https", "http / socks5"),
        ("miyaip_request_timeout_seconds", "61", "2-60"),
    ],
)
def test_invalid_miyaip_settings_are_rejected_even_while_cliproxy_is_active(
    field,
    value,
    message,
):
    _state, get_all, set_many = _config_store(
        {
            "task_proxy_mode": "dynamic",
            "dynamic_proxy_provider": "cliproxy",
        }
    )

    with get_all, set_many as set_many_mock:
        with pytest.raises(HTTPException) as error:
            config_api.update_config(config_api.ConfigUpdate(data={field: value}))

    assert error.value.status_code == 400
    assert message in str(error.value.detail)
    set_many_mock.assert_not_called()
