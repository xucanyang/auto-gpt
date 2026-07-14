from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.phone_api_relay import (
    RelayConflictError,
    RelayRedirectBlocked,
    RelayRegistry,
    RelayResponseTooLarge,
    RelayRoute,
    RelayValidationError,
    RelayFetchResult,
    create_relay_app,
    forwarded_api_url,
    normalize_public_origin,
    parse_source_api_url,
    resolve_public_addresses,
)


PUBLIC_DNS = lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))]


def test_forwarded_url_preserves_raw_path_and_query_bytes():
    source = "https://supplier.example:8443/api%2Fv1?z=2&z=1&a=%2F"
    route = parse_source_api_url(source)
    assert route.raw_suffix == "/api%2Fv1?z=2&z=1&a=%2F"
    assert forwarded_api_url(source, "https://phone-api.aa8.pl") == (
        "https://phone-api.aa8.pl/api%2Fv1?z=2&z=1&a=%2F"
    )

    root = parse_source_api_url("https://supplier.example")
    assert root.raw_suffix == "/"
    assert forwarded_api_url("https://supplier.example", "https://phone-api.aa8.pl") == "https://phone-api.aa8.pl/"


def test_public_origin_rejects_non_origin_values():
    for value in (
        "https://phone-api.aa8.pl/admin",
        "https://phone-api.aa8.pl/?token=x",
        "https://phone-api.aa8.pl/#fragment",
        "https://user:pass@phone-api.aa8.pl",
    ):
        with pytest.raises(RelayValidationError):
            normalize_public_origin(value)


def test_registry_rejects_same_suffix_from_different_origin(tmp_path: Path):
    registry = RelayRegistry(tmp_path / "relay.db")
    registry.set_config(enabled=True, active_origin="https://phone-api.aa8.pl", previous_origins=[])
    registry.sync_inventory(
        "plus-a",
        [{"pool_id": "1", "source_api_url": "https://one.example/api?x=1"}],
        resolver=PUBLIC_DNS,
    )
    with pytest.raises(RelayConflictError):
        registry.sync_inventory(
            "plus-b",
            [{"pool_id": "2", "source_api_url": "https://two.example/api?x=1"}],
            resolver=PUBLIC_DNS,
        )



def test_registry_rejects_relay_origin_as_source(tmp_path: Path):
    registry = RelayRegistry(tmp_path / "relay.db")
    registry.set_config(enabled=True, active_origin="https://phone-api.aa8.pl", previous_origins=[])
    with pytest.raises(RelayValidationError, match="relay origin"):
        registry.sync_inventory(
            "plus-a",
            [{"pool_id": "1", "source_api_url": "https://phone-api.aa8.pl/api?token=x"}],
            resolver=PUBLIC_DNS,
        )

def test_relay_get_head_host_allowlist_and_admin_auth(tmp_path: Path):
    calls: list[tuple[str, str]] = []

    def fetcher(route: RelayRoute, method: str) -> RelayFetchResult:
        calls.append((route.raw_suffix, method))
        return RelayFetchResult(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=b'{"ok":true}',
        )

    app = create_relay_app(
        db_path=tmp_path / "relay.db",
        admin_token="secret-token",
        resolver=PUBLIC_DNS,
        fetcher=fetcher,
    )
    with TestClient(app) as client:
        assert client.put(
            "/admin/v1/config",
            headers={"Authorization": "Bearer secret-token"},
            json={"enabled": True, "active_origin": "https://phone-api.aa8.pl", "previous_origins": []},
        ).status_code == 200
        assert client.put(
            "/admin/v1/inventory/plus-a",
            headers={"Authorization": "Bearer secret-token"},
            json={"items": [{"pool_id": "1", "source_api_url": "https://supplier.example/api%2Fv1?x=1&x=2"}]},
        ).status_code == 200

        response = client.get(
            "/api%2Fv1?x=1&x=2",
            headers={"Host": "phone-api.aa8.pl"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert calls == [("/api%2Fv1?x=1&x=2", "GET")]

        head = client.head(
            "/api%2Fv1?x=1&x=2",
            headers={"Host": "phone-api.aa8.pl"},
        )
        assert head.status_code == 200
        assert head.content == b""
        assert calls[-1] == ("/api%2Fv1?x=1&x=2", "HEAD")

        assert client.get("/api%2Fv1?x=1&x=2", headers={"Host": "other.example"}).status_code == 404
        assert client.get("/not-registered", headers={"Host": "phone-api.aa8.pl"}).status_code == 404
        assert client.get("/admin/v1/config").status_code == 401
        assert client.get(
            "/admin/v1/config", headers={"Authorization": "Bearer secret-token"}
        ).json()["active_origin"] == "https://phone-api.aa8.pl"


def test_relay_maps_redirect_and_oversize_to_502(tmp_path: Path):
    mode = {"kind": "redirect"}

    def fetcher(route: RelayRoute, method: str) -> RelayFetchResult:
        if mode["kind"] == "redirect":
            raise RelayRedirectBlocked("redirect")
        raise RelayResponseTooLarge("large")

    app = create_relay_app(
        db_path=tmp_path / "relay.db",
        admin_token="secret-token",
        resolver=PUBLIC_DNS,
        fetcher=fetcher,
    )
    with TestClient(app) as client:
        auth = {"Authorization": "Bearer secret-token"}
        assert client.put(
            "/admin/v1/config", headers=auth,
            json={"enabled": True, "active_origin": "https://phone-api.aa8.pl"},
        ).status_code == 200
        assert client.put(
            "/admin/v1/inventory/plus-a", headers=auth,
            json={"items": [{"pool_id": "1", "source_api_url": "https://supplier.example/api"}]},
        ).status_code == 200
        response = client.get("/api", headers={"Host": "phone-api.aa8.pl"})
        assert response.status_code == 502
        assert response.headers["X-Phone-Relay-Error"] == "redirect_blocked"
        mode["kind"] = "large"
        response = client.get("/api", headers={"Host": "phone-api.aa8.pl"})
        assert response.status_code == 502
        assert response.headers["X-Phone-Relay-Error"] == "response_too_large"


def test_private_dns_is_blocked():
    with pytest.raises(RelayValidationError):
        resolve_public_addresses("internal.example", 443, resolver=lambda h, p: [(2, 1, 6, "", ("10.0.0.10", p))])
    with pytest.raises(RelayValidationError):
        resolve_public_addresses("127.0.0.1", 80, resolver=PUBLIC_DNS)


def test_registry_tracks_plus_and_plus2_inventories_independently(tmp_path: Path):
    registry = RelayRegistry(tmp_path / "relay.db")
    registry.set_config(enabled=True, active_origin="https://phone-api.aa8.pl", previous_origins=[])
    plus = registry.sync_inventory(
        "auto-gpt-plus",
        [
            {"pool_id": "1", "source_api_url": "https://one.example/a?x=1"},
            {"pool_id": "2", "source_api_url": "https://two.example/b?x=2"},
        ],
        resolver=PUBLIC_DNS,
    )
    plus2 = registry.sync_inventory("auto-plus2", [], resolver=PUBLIC_DNS)
    assert plus["inventory_count"] == 2
    assert plus2["inventory_count"] == 0
    assert registry.inventory_status("auto-gpt-plus")["inventory_count"] == 2
    assert registry.inventory_status("auto-plus2")["inventory_count"] == 0
    assert registry.inventory_status()["owner_count"] == 2
