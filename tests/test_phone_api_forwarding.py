from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.chatgpt_core import phone_api_forwarding as forwarding
from services.chatgpt_core import phone_service
from services.chatgpt_core.phone_api_forwarding import PhoneApiForwardError, PhoneApiResolution
from services.chatgpt_core.phone_service import UploadedPhoneEntry, UploadedPhoneService, resolve_uploaded_phone_entry


@pytest.fixture(autouse=True)
def reset_forwarding_state(monkeypatch):
    forwarding.invalidate_forwarding_config_cache()
    with forwarding._LOCK:
        forwarding._SYNC_STATE.update({
            "status": "idle",
            "last_attempt_at": "",
            "last_success_at": "",
            "last_error": "",
            "inventory_count": 0,
            "route_count": 0,
            "owner_count": 0,
            "trigger": "",
        })
    monkeypatch.setenv("PHONE_API_RELAY_INTERNAL_URL", "http://relay:8787")
    monkeypatch.setenv("PHONE_API_RELAY_ADMIN_TOKEN", "test-token")
    monkeypatch.setenv("APP_INSTANCE_ID", "plus-test")
    yield
    forwarding.invalidate_forwarding_config_cache()


def test_resolve_phone_api_url_uses_public_origin_and_keeps_source(monkeypatch):
    monkeypatch.setattr(
        forwarding,
        "_relay_request",
        lambda *args, **kwargs: {
            "enabled": True,
            "active_origin": "https://phone-api.aa8.pl",
            "previous_origins": [],
        },
    )
    source = "https://supplier.example:8443/api%2Fv1?x=1&x=2"
    result = forwarding.resolve_phone_api_url(source, strict=True)
    assert result.source_api_url == source
    assert result.request_api_url == "https://phone-api.aa8.pl/api%2Fv1?x=1&x=2"
    assert result.source_api_host == "supplier.example"
    assert result.forwarded_api_host == "phone-api.aa8.pl"
    assert result.forwarded is True


def test_disabled_forwarding_restores_direct_source_url(monkeypatch):
    monkeypatch.setattr(
        forwarding,
        "_relay_request",
        lambda *args, **kwargs: {"enabled": False, "active_origin": "https://phone-api.aa8.pl"},
    )
    source = "https://supplier.example/api?token=demo"
    result = forwarding.resolve_phone_api_url(source, strict=True)
    assert result.request_api_url == source
    assert result.forwarded is False
    assert result.status == "disabled"


def test_uploaded_entry_never_falls_back_when_relay_config_is_unavailable(monkeypatch):
    def unavailable(*args, **kwargs):
        raise PhoneApiForwardError("relay down", code="relay_unavailable")

    monkeypatch.setattr(forwarding, "_relay_request", unavailable)
    entry = UploadedPhoneEntry(
        country_slug="uploaded",
        phone="+15551230001",
        detail_url="https://supplier.example/api?token=secret",
        api_url="https://supplier.example/api?token=secret",
        source_api_url="https://supplier.example/api?token=secret",
    )
    with pytest.raises(PhoneApiForwardError) as caught:
        resolve_uploaded_phone_entry(entry)
    assert caught.value.code == "relay_unavailable"


def test_uploaded_phone_poll_uses_frozen_forwarded_url(monkeypatch):
    source = "https://supplier.example/api?token=secret"
    forwarded_url = "https://phone-api.aa8.pl/api?token=secret"
    monkeypatch.setattr(
        phone_service,
        "resolve_phone_api_url",
        lambda value, strict=True: PhoneApiResolution(
            source_api_url=source,
            request_api_url=forwarded_url,
            source_api_host="supplier.example",
            forwarded_api_host="phone-api.aa8.pl",
            forwarded=True,
            status="forwarded",
        ),
    )
    seen: list[str] = []

    class Response:
        status_code = 200
        text = '{"data":{"code":"123456"}}'
        headers = {"X-Phone-Relay": "1"}

        @staticmethod
        def json():
            return {"data": {"code": "123456"}}

    monkeypatch.setattr(phone_service.requests, "get", lambda url, timeout: (seen.append(url) or Response()))
    entry = UploadedPhoneEntry(
        country_slug="uploaded",
        phone="+15551230001",
        detail_url=source,
        api_url=source,
        source_api_url=source,
        raw_line=f"+15551230001----{source}",
    )
    service = UploadedPhoneService([entry])
    result = service._fetch_api_poll_result(entry)
    assert result["code"] == "123456"
    assert seen == [forwarded_url]
    resolved = service.resolved_entry(entry)
    assert resolved is not None
    assert resolved.api_url == forwarded_url
    assert resolved.source_api_url == source
    assert resolved.raw_line == f"+15551230001----{forwarded_url}"


def test_relay_error_header_is_classified_as_forward_error(monkeypatch):
    source = "https://supplier.example/api?token=secret"
    forwarded_url = "https://phone-api.aa8.pl/api?token=secret"
    monkeypatch.setattr(
        phone_service,
        "resolve_phone_api_url",
        lambda value, strict=True: PhoneApiResolution(
            source_api_url=source,
            request_api_url=forwarded_url,
            source_api_host="supplier.example",
            forwarded_api_host="phone-api.aa8.pl",
            forwarded=True,
            status="forwarded",
        ),
    )
    response = SimpleNamespace(
        status_code=502,
        text='{"error":"phone_api_relay_error"}',
        headers={"X-Phone-Relay": "1", "X-Phone-Relay-Error": "upstream_unavailable"},
        json=lambda: {"error": "phone_api_relay_error"},
    )
    monkeypatch.setattr(phone_service.requests, "get", lambda *args, **kwargs: response)
    entry = UploadedPhoneEntry("uploaded", "+15551230001", source, source, source_api_url=source)
    service = UploadedPhoneService([entry])
    with pytest.raises(PhoneApiForwardError) as caught:
        service._fetch_api_poll_result(entry)
    assert caught.value.code == "upstream_unavailable"


def test_phone_pool_persists_source_url_and_forward_error_keeps_number_active(monkeypatch):
    from sqlmodel import SQLModel, create_engine

    from core import db as core_db
    from services.chatgpt_core import phone_pool_repository as repo_module
    from services.chatgpt_core.phone_pool_repository import PhonePoolRepository

    engine = create_engine("sqlite://")
    monkeypatch.setattr(core_db, "engine", engine)
    monkeypatch.setattr(repo_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    core_db._ensure_phone_pool_schema()

    source = "https://supplier.example/api?token=secret"
    forwarded_url = "https://phone-api.aa8.pl/api?token=secret"
    monkeypatch.setattr(
        forwarding,
        "resolve_phone_api_url",
        lambda value, strict=True: PhoneApiResolution(
            source_api_url=source,
            request_api_url=forwarded_url,
            source_api_host="supplier.example",
            forwarded_api_host="phone-api.aa8.pl",
            forwarded=True,
            status="forwarded",
        ),
    )

    repo = PhonePoolRepository()
    created = repo.add(phone="+15551230001", api_url=source)
    assert created is not None
    item = repo.to_phone_items([created])[0]
    assert item["api_url"] == forwarded_url
    assert item["source_api_url"] == source
    assert repo.get("+15551230001").api_url == source

    failed = repo.record_task_status(
        "+15551230001",
        "api_forward_error",
        reason="Relay unavailable",
    )
    assert failed is not None
    assert failed.status == "active"
    assert failed.last_error_code == "api_forward_error"


def test_api_expiry_probe_uses_forwarded_url(monkeypatch):
    from sqlmodel import SQLModel, create_engine

    from core import db as core_db
    from services.chatgpt_core import phone_pool_repository as repo_module
    from services.chatgpt_core.phone_pool_repository import PhonePoolRepository

    engine = create_engine("sqlite://")
    monkeypatch.setattr(core_db, "engine", engine)
    monkeypatch.setattr(repo_module, "engine", engine)
    SQLModel.metadata.create_all(engine)
    core_db._ensure_phone_pool_schema()

    source = "https://supplier.example/api?token=secret"
    forwarded_url = "https://phone-api.aa8.pl/api?token=secret"
    monkeypatch.setattr(
        forwarding,
        "resolve_phone_api_url",
        lambda value, strict=True: PhoneApiResolution(
            source_api_url=source,
            request_api_url=forwarded_url,
            source_api_host="supplier.example",
            forwarded_api_host="phone-api.aa8.pl",
            forwarded=True,
            status="forwarded",
        ),
    )
    seen: list[str] = []
    response = SimpleNamespace(
        status_code=200,
        text='{"data":{"expired_date":"2026-12-31"}}',
        json=lambda: {"data": {"expired_date": "2026-12-31"}},
    )
    monkeypatch.setattr(repo_module.requests, "get", lambda url, timeout: (seen.append(url) or response))

    repo = PhonePoolRepository()
    record = repo.add(phone="+15551230001", api_url=source)
    result = repo.refresh_api_expiry_for_ids([record.id])
    assert result["summary"]["success"] == 1
    assert seen == [forwarded_url]
    assert repo.get("+15551230001").api_url == source


def test_inventory_conflict_blocks_forwarding_instead_of_using_wrong_route(monkeypatch):
    monkeypatch.setattr(
        forwarding,
        "_relay_request",
        lambda *args, **kwargs: {
            "enabled": True,
            "active_origin": "https://phone-api.aa8.pl",
            "previous_origins": [],
        },
    )
    with forwarding._LOCK:
        forwarding._SYNC_STATE.update(
            {
                "status": "conflict",
                "last_attempt_at": "2026-07-14T00:00:00Z",
                "last_error": "手机号 API 转发路径冲突",
            }
        )
    source = "https://supplier.example/api?token=secret"
    with pytest.raises(PhoneApiForwardError) as caught:
        forwarding.resolve_phone_api_url(source, strict=True)
    assert caught.value.code == "route_conflict"
    fields = forwarding.serialize_forwarding_fields(source)
    assert fields["forwarded_api_url"] == ""
    assert fields["forward_status"] == "conflict"
