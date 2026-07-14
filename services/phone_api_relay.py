"""Central, registry-backed relay for phone/SMS polling APIs.

The relay is intentionally standalone: application instances only talk to its
admin HTTP API and never open the registry SQLite file directly.

Run with either::

    uvicorn services.phone_api_relay:create_relay_app --factory --no-access-log

or::

    python -m services.phone_api_relay
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import time
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import SplitResult, urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field


LOGGER = logging.getLogger("phone_api_relay")

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 12.0
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_PUBLIC_ORIGIN = "https://phone-api.aa8.pl"
MAX_INVENTORY_ITEMS = 20_000

_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_BLOCKED_METADATA_HOSTS = {
    "instance-data.ec2.internal",
    "metadata",
    "metadata.azure.internal",
    "metadata.google.internal",
    "metadata.google",
}


class RelayValidationError(ValueError):
    """An admin-supplied config or inventory value is invalid."""


class RelayConflictError(RuntimeError):
    """The same raw suffix maps to two different source origins."""

    def __init__(self, route_hash: str, message: str = "route conflict") -> None:
        super().__init__(message)
        self.route_hash = str(route_hash or "")


class RelayUpstreamError(RuntimeError):
    """A safe upstream connection could not be completed."""


class RelayResponseTooLarge(RelayUpstreamError):
    pass


class RelayRedirectBlocked(RelayUpstreamError):
    pass


def _utcnow_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clamped_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(str(value or "").strip())
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _clamped_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(str(value or "").strip()))
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _env_bool(value: Any, default: bool = False) -> bool:
    """Parse a conventional boolean environment value without guessing."""

    text = str(value if value is not None else "").strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def _format_authority(hostname: str, port: int | None, scheme: str) -> str:
    host = str(hostname or "").strip().lower().rstrip(".")
    if not host:
        raise RelayValidationError("URL host is required")
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        rendered = host
    else:
        rendered = f"[{host}]" if ip.version == 6 else host
    default_port = 443 if scheme == "https" else 80
    return f"{rendered}:{port}" if port and port != default_port else rendered


def normalize_public_origin(value: Any) -> str:
    """Normalize an externally visible relay origin.

    Only an origin is accepted. A path prefix would violate the contract that
    source raw paths remain byte-for-byte unchanged.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        port = parts.port
    except (TypeError, ValueError) as exc:
        raise RelayValidationError("relay origin is invalid") from exc
    scheme = str(parts.scheme or "").lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        raise RelayValidationError("relay origin must use http(s)")
    if parts.username or parts.password:
        raise RelayValidationError("relay origin must not contain credentials")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise RelayValidationError("relay origin must not contain path, query, or fragment")
    return f"{scheme}://{_format_authority(parts.hostname, port, scheme)}"


def normalize_origin_list(values: Iterable[Any] | None, *, active_origin: str = "") -> list[str]:
    active = normalize_public_origin(active_origin)
    result: list[str] = []
    seen: set[str] = {active} if active else set()
    for value in values or []:
        normalized = normalize_public_origin(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= 32:
            break
    return result


@dataclass(frozen=True, slots=True)
class RelayRoute:
    route_hash: str
    raw_suffix: str
    source_origin: str
    source_scheme: str
    source_host: str
    source_port: int

    @property
    def source_url(self) -> str:
        return f"{self.source_origin}{self.raw_suffix}"


def parse_source_api_url(value: Any) -> RelayRoute:
    """Parse a source API URL without decoding or re-encoding its suffix."""

    text = str(value or "").strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except (TypeError, ValueError) as exc:
        raise RelayValidationError("source API URL is invalid") from exc
    scheme = str(parts.scheme or "").lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        raise RelayValidationError("source API URL must use http(s)")
    if parts.username or parts.password:
        raise RelayValidationError("source API URL credentials are not supported")
    if parts.fragment:
        raise RelayValidationError("source API URL fragment is not supported")
    raw_path = parts.path or "/"
    raw_suffix = f"{raw_path}?{parts.query}" if parts.query else raw_path
    route_hash = hashlib.sha256(raw_suffix.encode("utf-8")).hexdigest()
    resolved_port = int(port or (443 if scheme == "https" else 80))
    origin = f"{scheme}://{_format_authority(parts.hostname, resolved_port, scheme)}"
    return RelayRoute(
        route_hash=route_hash,
        raw_suffix=raw_suffix,
        source_origin=origin,
        source_scheme=scheme,
        source_host=str(parts.hostname or "").lower().rstrip("."),
        source_port=resolved_port,
    )


def raw_suffix_from_scope(scope: dict[str, Any]) -> tuple[str, str]:
    raw_path = scope.get("raw_path") or b"/"
    query = scope.get("query_string") or b""
    if isinstance(raw_path, str):
        raw_path_bytes = raw_path.encode("latin-1", errors="surrogateescape")
    else:
        raw_path_bytes = bytes(raw_path)
    if isinstance(query, str):
        query_bytes = query.encode("latin-1", errors="surrogateescape")
    else:
        query_bytes = bytes(query)
    suffix_bytes = raw_path_bytes + (b"?" + query_bytes if query_bytes else b"")
    suffix = suffix_bytes.decode("latin-1")
    return suffix, hashlib.sha256(suffix_bytes).hexdigest()


def route_hash_for_url(value: Any) -> str:
    return parse_source_api_url(value).route_hash


def forwarded_api_url(source_api_url: Any, active_origin: Any) -> str:
    route = parse_source_api_url(source_api_url)
    origin = normalize_public_origin(active_origin)
    if not origin:
        raise RelayValidationError("active relay origin is required")
    return f"{origin}{route.raw_suffix}"


def _default_dns_resolver(host: str, port: int) -> Sequence[Any]:
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def resolve_public_addresses(
    host: str,
    port: int,
    *,
    resolver: Callable[[str, int], Sequence[Any]] | None = None,
) -> list[str]:
    """Resolve a target and require every returned address to be public.

    Requiring all answers to be public prevents a mixed public/private DNS set
    from becoming a rebinding escape hatch. The returned IPs are subsequently
    pinned for the actual socket connection.
    """

    hostname = str(host or "").strip().lower().rstrip(".")
    if not hostname or hostname in _BLOCKED_METADATA_HOSTS or hostname.endswith(".localhost"):
        raise RelayValidationError("source host is not public")
    if hostname == "localhost":
        raise RelayValidationError("source host is not public")
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise RelayValidationError("source host is not public")

    resolve = resolver or _default_dns_resolver
    try:
        answers = list(resolve(hostname, int(port)))
    except Exception as exc:
        raise RelayValidationError("source host could not be resolved") from exc
    addresses: list[str] = []
    seen: set[str] = set()
    for answer in answers:
        try:
            sockaddr = answer[4] if isinstance(answer, tuple) and len(answer) >= 5 else answer
            raw_ip = sockaddr[0] if isinstance(sockaddr, tuple) else str(sockaddr)
            ip = ipaddress.ip_address(str(raw_ip).split("%", 1)[0])
        except Exception as exc:
            raise RelayValidationError("source DNS answer is invalid") from exc
        if not ip.is_global:
            raise RelayValidationError("source DNS resolved to a non-public address")
        rendered = str(ip)
        if rendered not in seen:
            seen.add(rendered)
            addresses.append(rendered)
    if not addresses:
        raise RelayValidationError("source host has no public address")
    return addresses


class RelayRegistry:
    """SQLite registry owned exclusively by the relay process."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS relay_config (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    active_origin TEXT NOT NULL DEFAULT '',
                    previous_origins_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                INSERT OR IGNORE INTO relay_config(singleton) VALUES (1);

                CREATE TABLE IF NOT EXISTS relay_routes (
                    route_hash TEXT PRIMARY KEY,
                    raw_suffix TEXT NOT NULL UNIQUE,
                    source_origin TEXT NOT NULL,
                    source_scheme TEXT NOT NULL,
                    source_host TEXT NOT NULL,
                    source_port INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS relay_route_owners (
                    instance_id TEXT NOT NULL,
                    pool_id TEXT NOT NULL,
                    route_hash TEXT NOT NULL REFERENCES relay_routes(route_hash) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(instance_id, pool_id)
                );
                CREATE INDEX IF NOT EXISTS idx_relay_route_owners_route_hash
                    ON relay_route_owners(route_hash);
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def initialize_from_environment(self, *, enabled: bool, active_origin: Any) -> dict[str, Any]:
        """Seed a fresh registry from process environment without clobbering edits.

        The relay DB is persistent. Environment values are therefore defaults only:
        they are applied when the singleton row has never been configured (empty
        ``updated_at``). A later restart or env change cannot silently overwrite an
        administrator's explicit configuration.
        """

        origin = normalize_public_origin(active_origin)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT enabled, active_origin, previous_origins_json, updated_at "
                "FROM relay_config WHERE singleton = 1"
            ).fetchone()
            if row is None or str(row["updated_at"] or "").strip():
                return self.get_config()
            now = _utcnow_text()
            conn.execute(
                """
                UPDATE relay_config
                   SET enabled = ?, active_origin = ?, previous_origins_json = '[]', updated_at = ?
                 WHERE singleton = 1
                """,
                (1 if enabled else 0, origin, now),
            )
            conn.commit()
        return self.get_config()

    def get_config(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM relay_config WHERE singleton = 1").fetchone()
        try:
            previous = json.loads(str(row["previous_origins_json"] or "[]")) if row else []
        except Exception:
            previous = []
        return {
            "enabled": bool(int(row["enabled"] or 0)) if row else False,
            "active_origin": str(row["active_origin"] or "") if row else "",
            "previous_origins": [str(item) for item in previous if str(item or "").strip()],
            "updated_at": str(row["updated_at"] or "") if row else "",
        }

    def set_config(self, *, enabled: bool, active_origin: Any, previous_origins: Iterable[Any] | None) -> dict[str, Any]:
        active = normalize_public_origin(active_origin)
        previous = normalize_origin_list(previous_origins, active_origin=active)
        if enabled and not active:
            raise RelayValidationError("active relay origin is required when enabled")
        now = _utcnow_text()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE relay_config
                   SET enabled = ?, active_origin = ?, previous_origins_json = ?, updated_at = ?
                 WHERE singleton = 1
                """,
                (1 if enabled else 0, active, json.dumps(previous, separators=(",", ":")), now),
            )
            conn.commit()
        return self.get_config()

    def sync_inventory(
        self,
        instance_id: Any,
        items: Iterable[dict[str, Any]],
        *,
        resolver: Callable[[str, int], Sequence[Any]] | None = None,
    ) -> dict[str, Any]:
        instance = str(instance_id or "").strip()
        if not _INSTANCE_ID_RE.fullmatch(instance):
            raise RelayValidationError("instance_id is invalid")
        raw_items = list(items or [])
        if len(raw_items) > MAX_INVENTORY_ITEMS:
            raise RelayValidationError("inventory is too large")

        prepared: list[tuple[str, RelayRoute]] = []
        by_pool: set[str] = set()
        by_hash: dict[str, RelayRoute] = {}
        validated_origins: set[str] = set()
        config = self.get_config()
        relay_hosts = {
            str(urlsplit(str(origin or "")).hostname or "").lower().rstrip(".")
            for origin in [config.get("active_origin") or "", *(config.get("previous_origins") or [])]
            if str(origin or "").strip()
        }
        relay_hosts.discard("")
        for item in raw_items:
            pool_id = str((item or {}).get("pool_id") or "").strip()
            if not pool_id or len(pool_id) > 128 or pool_id in by_pool:
                raise RelayValidationError("pool_id is empty or duplicated")
            route = parse_source_api_url((item or {}).get("source_api_url"))
            if route.source_host in relay_hosts:
                raise RelayValidationError("source API URL must not target a relay origin")
            previous = by_hash.get(route.route_hash)
            if previous and (previous.raw_suffix != route.raw_suffix or previous.source_origin != route.source_origin):
                raise RelayConflictError(route.route_hash)
            if route.source_origin not in validated_origins:
                resolve_public_addresses(route.source_host, route.source_port, resolver=resolver)
                validated_origins.add(route.source_origin)
            by_pool.add(pool_id)
            by_hash[route.route_hash] = route
            prepared.append((pool_id, route))

        now = _utcnow_text()
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM relay_route_owners WHERE instance_id = ?", (instance,))
                conn.execute(
                    "DELETE FROM relay_routes WHERE NOT EXISTS "
                    "(SELECT 1 FROM relay_route_owners o WHERE o.route_hash = relay_routes.route_hash)"
                )
                for pool_id, route in prepared:
                    existing = conn.execute(
                        "SELECT raw_suffix, source_origin FROM relay_routes WHERE route_hash = ?",
                        (route.route_hash,),
                    ).fetchone()
                    if existing and (
                        str(existing["raw_suffix"] or "") != route.raw_suffix
                        or str(existing["source_origin"] or "") != route.source_origin
                    ):
                        raise RelayConflictError(route.route_hash)
                    if existing is None:
                        same_suffix = conn.execute(
                            "SELECT route_hash, source_origin FROM relay_routes WHERE raw_suffix = ?",
                            (route.raw_suffix,),
                        ).fetchone()
                        if same_suffix and str(same_suffix["source_origin"] or "") != route.source_origin:
                            raise RelayConflictError(str(same_suffix["route_hash"] or route.route_hash))
                        conn.execute(
                            """
                            INSERT INTO relay_routes(
                                route_hash, raw_suffix, source_origin, source_scheme,
                                source_host, source_port, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                route.route_hash,
                                route.raw_suffix,
                                route.source_origin,
                                route.source_scheme,
                                route.source_host,
                                route.source_port,
                                now,
                                now,
                            ),
                        )
                    conn.execute(
                        """
                        INSERT INTO relay_route_owners(
                            instance_id, pool_id, route_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (instance, pool_id, route.route_hash, now, now),
                    )
                conn.execute(
                    "DELETE FROM relay_routes WHERE NOT EXISTS "
                    "(SELECT 1 FROM relay_route_owners o WHERE o.route_hash = relay_routes.route_hash)"
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.inventory_status(instance)

    def inventory_status(self, instance_id: Any = "") -> dict[str, Any]:
        instance = str(instance_id or "").strip()
        with self._connect() as conn:
            route_count = int(conn.execute("SELECT COUNT(*) FROM relay_routes").fetchone()[0])
            owner_count = int(conn.execute("SELECT COUNT(*) FROM relay_route_owners").fetchone()[0])
            instance_owner_count = 0
            if instance:
                instance_owner_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM relay_route_owners WHERE instance_id = ?",
                        (instance,),
                    ).fetchone()[0]
                )
        return {
            "instance_id": instance,
            "inventory_count": instance_owner_count,
            "route_count": route_count,
            "owner_count": owner_count,
        }

    def lookup(self, route_hash: str, raw_suffix: str) -> RelayRoute | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM relay_routes WHERE route_hash = ?",
                (str(route_hash or ""),),
            ).fetchone()
        if row is None or str(row["raw_suffix"] or "") != str(raw_suffix or ""):
            return None
        return RelayRoute(
            route_hash=str(row["route_hash"] or ""),
            raw_suffix=str(row["raw_suffix"] or ""),
            source_origin=str(row["source_origin"] or ""),
            source_scheme=str(row["source_scheme"] or ""),
            source_host=str(row["source_host"] or ""),
            source_port=int(row["source_port"] or 0),
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, connect_host: str, port: int, *, server_hostname: str, timeout: float) -> None:
        super().__init__(connect_host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._relay_server_hostname = server_hostname

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._relay_server_hostname)


@dataclass(frozen=True, slots=True)
class RelayFetchResult:
    status_code: int
    headers: dict[str, str]
    body: bytes


def fetch_relay_upstream(
    route: RelayRoute,
    method: str,
    *,
    resolver: Callable[[str, int], Sequence[Any]] | None = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> RelayFetchResult:
    addresses = resolve_public_addresses(route.source_host, route.source_port, resolver=resolver)
    last_error: Exception | None = None
    for address in addresses:
        conn: http.client.HTTPConnection | None = None
        try:
            if route.source_scheme == "https":
                conn = _PinnedHTTPSConnection(
                    address,
                    route.source_port,
                    server_hostname=route.source_host,
                    timeout=connect_timeout,
                )
            else:
                conn = http.client.HTTPConnection(address, route.source_port, timeout=connect_timeout)
            conn.connect()
            if conn.sock is not None:
                conn.sock.settimeout(read_timeout)
            authority = _format_authority(route.source_host, route.source_port, route.source_scheme)
            conn.request(
                str(method or "GET").upper(),
                route.raw_suffix,
                headers={
                    "Host": authority,
                    "Accept": "application/json, text/plain, */*",
                    "Connection": "close",
                    "User-Agent": "auto-gpt-phone-api-relay/1.0",
                },
            )
            upstream = conn.getresponse()
            status_code = int(upstream.status or 502)
            if 300 <= status_code < 400:
                raise RelayRedirectBlocked("upstream redirect is forbidden")
            body = b"" if str(method or "GET").upper() == "HEAD" else upstream.read(max_response_bytes + 1)
            if len(body) > max_response_bytes:
                raise RelayResponseTooLarge("upstream response is too large")
            content_type = str(upstream.getheader("Content-Type") or "application/octet-stream")[:200]
            cache_control = str(upstream.getheader("Cache-Control") or "no-store")[:200]
            return RelayFetchResult(
                status_code=status_code,
                headers={"Content-Type": content_type, "Cache-Control": cache_control},
                body=body,
            )
        except (RelayRedirectBlocked, RelayResponseTooLarge):
            raise
        except Exception as exc:
            last_error = exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    raise RelayUpstreamError("upstream connection failed") from last_error


class RelayConfigUpdate(BaseModel):
    enabled: bool = False
    active_origin: str = ""
    previous_origins: list[str] = Field(default_factory=list)


class RelayInventoryItem(BaseModel):
    pool_id: str | int
    source_api_url: str


class RelayInventorySync(BaseModel):
    items: list[RelayInventoryItem] = Field(default_factory=list)


def _extract_bearer_token(request: Request) -> str:
    auth = str(request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return str(request.headers.get("x-relay-admin-token") or "").strip()


def _authority_from_host_header(value: str) -> tuple[str, int | None]:
    text = str(value or "").strip()
    if not text:
        return "", None
    try:
        parsed = urlsplit(f"//{text}")
        return str(parsed.hostname or "").lower().rstrip("."), parsed.port
    except ValueError:
        return "", None


def _origin_authority(value: str) -> tuple[str, int]:
    parts = urlsplit(normalize_public_origin(value))
    return str(parts.hostname or "").lower().rstrip("."), int(parts.port or (443 if parts.scheme == "https" else 80))


def _host_allowed(host_header: str, origins: Iterable[str]) -> bool:
    request_host, request_port = _authority_from_host_header(host_header)
    if not request_host:
        return False
    for origin in origins:
        origin_host, origin_port = _origin_authority(origin)
        if request_host != origin_host:
            continue
        if request_port is None or request_port == origin_port:
            return True
    return False


def _public_error(status_code: int, code: str, route_hash: str = "") -> JSONResponse:
    payload = {"error": "phone_api_relay_error"}
    if route_hash:
        payload["route_id"] = route_hash[:16]
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers={
            "Cache-Control": "no-store",
            "X-Phone-Relay": "1",
            "X-Phone-Relay-Error": str(code or "relay_error")[:64],
        },
    )


def create_relay_app(
    *,
    db_path: str | Path | None = None,
    admin_token: str | None = None,
    resolver: Callable[[str, int], Sequence[Any]] | None = None,
    fetcher: Callable[[RelayRoute, str], RelayFetchResult] | None = None,
    connect_timeout: float | None = None,
    read_timeout: float | None = None,
    max_response_bytes: int | None = None,
) -> FastAPI:
    resolved_db_path = Path(
        db_path
        or os.getenv("PHONE_API_RELAY_DB")
        or "/runtime/phone_api_relay.db"
    )
    registry = RelayRegistry(resolved_db_path)
    configured_token = str(admin_token if admin_token is not None else os.getenv("PHONE_API_RELAY_ADMIN_TOKEN") or "").strip()
    configured_public_origin = str(
        os.getenv("PHONE_API_RELAY_PUBLIC_ORIGIN") or DEFAULT_PUBLIC_ORIGIN
    ).strip()
    configured_enabled = _env_bool(os.getenv("PHONE_API_RELAY_ENABLED"), default=False)
    try:
        registry.initialize_from_environment(
            enabled=configured_enabled,
            active_origin=configured_public_origin,
        )
    except RelayValidationError:
        # Invalid operator configuration must fail closed at startup rather than
        # creating a relay that could accept an unvalidated Host header.
        raise
    resolved_connect_timeout = _clamped_float(
        connect_timeout if connect_timeout is not None else os.getenv("PHONE_API_RELAY_CONNECT_TIMEOUT_SECONDS"),
        DEFAULT_CONNECT_TIMEOUT_SECONDS,
        minimum=0.5,
        maximum=30.0,
    )
    resolved_read_timeout = _clamped_float(
        read_timeout if read_timeout is not None else os.getenv("PHONE_API_RELAY_READ_TIMEOUT_SECONDS"),
        DEFAULT_READ_TIMEOUT_SECONDS,
        minimum=0.5,
        maximum=60.0,
    )
    resolved_max_response_bytes = _clamped_int(
        max_response_bytes if max_response_bytes is not None else os.getenv("PHONE_API_RELAY_MAX_RESPONSE_BYTES"),
        DEFAULT_MAX_RESPONSE_BYTES,
        minimum=1024,
        maximum=8 * 1024 * 1024,
    )

    app = FastAPI(title="Phone API Relay", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.registry = registry

    def require_admin(request: Request) -> None:
        supplied = _extract_bearer_token(request)
        if not configured_token or not supplied or not hmac.compare_digest(supplied, configured_token):
            raise HTTPException(status_code=401, detail="admin authentication required")

    @app.get("/health", include_in_schema=False)
    @app.get("/healthz", include_in_schema=False)
    def health() -> Response:
        # Deliberately do not expose relay config, origins, registry counts, or
        # admin-token state on this unauthenticated liveness endpoint.
        return Response(
            content=b"{\"ok\":true}",
            media_type="application/json",
            headers={"Cache-Control": "no-store", "X-Phone-Relay": "1"},
        )

    @app.get("/admin/v1/config", dependencies=[Depends(require_admin)])
    def get_admin_config():
        return {"ok": True, **registry.get_config()}

    @app.put("/admin/v1/config", dependencies=[Depends(require_admin)])
    def put_admin_config(body: RelayConfigUpdate):
        try:
            value = registry.set_config(
                enabled=body.enabled,
                active_origin=body.active_origin,
                previous_origins=body.previous_origins,
            )
        except RelayValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, **value}

    @app.get("/admin/v1/inventory/{instance_id}", dependencies=[Depends(require_admin)])
    def get_inventory_status(instance_id: str):
        try:
            if not _INSTANCE_ID_RE.fullmatch(str(instance_id or "")):
                raise RelayValidationError("instance_id is invalid")
            return {"ok": True, **registry.inventory_status(instance_id)}
        except RelayValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/admin/v1/inventory/{instance_id}", dependencies=[Depends(require_admin)])
    def put_inventory(instance_id: str, body: RelayInventorySync):
        try:
            result = registry.sync_inventory(
                instance_id,
                [item.model_dump() for item in body.items],
                resolver=resolver,
            )
        except RelayConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "route_conflict", "route_id": exc.route_hash[:16]},
            ) from exc
        except RelayValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.api_route("/{public_path:path}", methods=["GET", "HEAD"])
    def relay_public(public_path: str, request: Request):
        del public_path
        started = time.monotonic()
        raw_suffix, route_hash = raw_suffix_from_scope(request.scope)
        response_status = 500
        try:
            config = registry.get_config()
            if not config.get("enabled"):
                response_status = 503
                return _public_error(503, "disabled", route_hash)
            origins = [config.get("active_origin") or "", *(config.get("previous_origins") or [])]
            if not _host_allowed(str(request.headers.get("host") or ""), origins):
                response_status = 404
                return _public_error(404, "host_not_registered", route_hash)
            route = registry.lookup(route_hash, raw_suffix)
            if route is None:
                response_status = 404
                return _public_error(404, "route_not_registered", route_hash)
            relay_hosts = {
                _origin_authority(str(origin))[0]
                for origin in origins
                if str(origin or "").strip()
            }
            if route.source_host in relay_hosts:
                response_status = 502
                return _public_error(502, "recursive_route_blocked", route_hash)
            try:
                if fetcher is not None:
                    result = fetcher(route, request.method)
                else:
                    result = fetch_relay_upstream(
                        route,
                        request.method,
                        resolver=resolver,
                        connect_timeout=resolved_connect_timeout,
                        read_timeout=resolved_read_timeout,
                        max_response_bytes=resolved_max_response_bytes,
                    )
            except RelayRedirectBlocked:
                response_status = 502
                return _public_error(502, "redirect_blocked", route_hash)
            except RelayResponseTooLarge:
                response_status = 502
                return _public_error(502, "response_too_large", route_hash)
            except (RelayValidationError, RelayUpstreamError, OSError, TimeoutError):
                response_status = 502
                return _public_error(502, "upstream_unavailable", route_hash)
            response_status = int(result.status_code or 502)
            headers = {
                **dict(result.headers or {}),
                "X-Phone-Relay": "1",
                "X-Phone-Relay-Route": route_hash[:16],
            }
            return Response(
                content=b"" if request.method == "HEAD" else bytes(result.body or b""),
                status_code=response_status,
                headers=headers,
            )
        finally:
            LOGGER.info(
                "phone_api_relay route=%s status=%s elapsed_ms=%s",
                route_hash,
                response_status,
                max(int((time.monotonic() - started) * 1000), 0),
            )

    return app


def run_relay() -> None:
    import uvicorn

    host = str(os.getenv("PHONE_API_RELAY_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = _clamped_int(os.getenv("PHONE_API_RELAY_PORT"), 8787, minimum=1, maximum=65535)
    uvicorn.run(
        create_relay_app(),
        host=host,
        port=port,
        access_log=False,
        log_level=str(os.getenv("PHONE_API_RELAY_LOG_LEVEL") or "info").strip().lower() or "info",
    )


if __name__ == "__main__":
    run_relay()

