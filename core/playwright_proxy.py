from __future__ import annotations

import socket
import socketserver
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, Optional
from urllib.parse import unquote, urlsplit

from .proxy_utils import build_playwright_proxy_config


_MAX_CONNECT_HEADER_BYTES = 64 * 1024
_CONNECT_TIMEOUT_SECONDS = 20
_RELAY_TIMEOUT_SECONDS = 90


def _parse_connect_target(target: str) -> tuple[str, int]:
    value = str(target or "").strip()
    if value.startswith("["):
        closing = value.find("]")
        if closing <= 1 or closing + 1 >= len(value) or value[closing + 1] != ":":
            raise ValueError("invalid IPv6 CONNECT target")
        host = value[1:closing]
        port_text = value[closing + 2 :]
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator:
            raise ValueError("CONNECT target is missing a port")
    port = int(port_text)
    if not host or port < 1 or port > 65535:
        raise ValueError("invalid CONNECT target")
    return host, port


class _ThreadingConnectServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64


class _Socks5ConnectHandler(socketserver.BaseRequestHandler):
    def _send_error(self, status: bytes) -> None:
        try:
            self.request.sendall(
                b"HTTP/1.1 " + status + b"\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
            )
        except OSError:
            pass

    @staticmethod
    def _pump(source: socket.socket, target: socket.socket, stopped: threading.Event) -> None:
        try:
            while not stopped.is_set():
                data = source.recv(64 * 1024)
                if not data:
                    break
                target.sendall(data)
        except (OSError, TimeoutError):
            pass
        finally:
            stopped.set()
            try:
                target.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def handle(self) -> None:
        client = self.request
        client.settimeout(_CONNECT_TIMEOUT_SECONDS)
        header = bytearray()
        while b"\r\n\r\n" not in header:
            chunk = client.recv(4096)
            if not chunk:
                return
            header.extend(chunk)
            if len(header) > _MAX_CONNECT_HEADER_BYTES:
                self._send_error(b"431 Request Header Fields Too Large")
                return

        raw_header, buffered = bytes(header).split(b"\r\n\r\n", 1)
        try:
            request_line = raw_header.split(b"\r\n", 1)[0].decode("latin-1")
            method, target, _http_version = request_line.split(" ", 2)
            if method.upper() != "CONNECT":
                self._send_error(b"405 Method Not Allowed")
                return
            target_host, target_port = _parse_connect_target(target)
        except (UnicodeError, ValueError):
            self._send_error(b"400 Bad Request")
            return

        upstream = None
        tunnel_established = False
        server = self.server
        try:
            import socks

            upstream = socks.create_connection(
                (target_host, target_port),
                proxy_type=socks.SOCKS5,
                proxy_addr=server.upstream_host,
                proxy_port=server.upstream_port,
                proxy_username=server.upstream_username,
                proxy_password=server.upstream_password,
                proxy_rdns=server.proxy_rdns,
                timeout=_CONNECT_TIMEOUT_SECONDS,
            )
            upstream.settimeout(_RELAY_TIMEOUT_SECONDS)
            client.settimeout(_RELAY_TIMEOUT_SECONDS)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            tunnel_established = True
            if buffered:
                upstream.sendall(buffered)

            stopped = threading.Event()
            client_to_upstream = threading.Thread(
                target=self._pump,
                args=(client, upstream, stopped),
                name="playwright-proxy-client-upstream",
                daemon=True,
            )
            client_to_upstream.start()
            self._pump(upstream, client, stopped)
            stopped.set()
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client_to_upstream.join(timeout=2)
        except (OSError, TimeoutError, ValueError):
            if not tunnel_established:
                self._send_error(b"502 Bad Gateway")
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass


class _AuthenticatedSocks5ConnectBridge:
    def __init__(self, proxy_url: str) -> None:
        parts = urlsplit(str(proxy_url or "").strip())
        if parts.scheme.lower() not in {"socks5", "socks5h"}:
            raise ValueError("authenticated SOCKS5 bridge requires a SOCKS5 proxy")
        if not parts.hostname or parts.port is None:
            raise ValueError("authenticated SOCKS5 proxy must include host and port")

        self._upstream_host = parts.hostname
        self._upstream_port = parts.port
        self._upstream_username = unquote(parts.username) if parts.username else None
        self._upstream_password = unquote(parts.password) if parts.password else None
        # Project proxy normalization treats socks5 as remote-DNS socks5h.
        # Keep that invariant here too, including direct callers that bypass it.
        self._proxy_rdns = True
        self._server: Optional[_ThreadingConnectServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def server_url(self) -> str:
        if self._server is None:
            raise RuntimeError("SOCKS5 bridge has not started")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        if self._server is not None:
            return
        server = _ThreadingConnectServer(("127.0.0.1", 0), _Socks5ConnectHandler)
        server.upstream_host = self._upstream_host
        server.upstream_port = self._upstream_port
        server.upstream_username = self._upstream_username
        server.upstream_password = self._upstream_password
        server.proxy_rdns = self._proxy_rdns
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="playwright-authenticated-socks5-bridge",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=3)


@contextmanager
def playwright_proxy_context(
    proxy_url: Optional[str],
    *,
    logger: Optional[Callable[[str], None]] = None,
) -> Iterator[Optional[dict[str, str]]]:
    """Yield a Playwright proxy config with authenticated SOCKS5 compatibility.

    Chromium does not support SOCKS5 username/password authentication and does
    not recognize the requests-specific ``socks5h`` scheme. For that exact
    shape, expose a short-lived loopback-only HTTP CONNECT proxy and forward its
    tunnels through the original authenticated SOCKS5 upstream.
    """

    value = str(proxy_url or "").strip()
    if not value:
        yield None
        return

    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    has_auth = bool(parts.username or parts.password)
    log = logger or (lambda _message: None)

    if scheme in {"socks5", "socks5h"} and has_auth:
        bridge = _AuthenticatedSocks5ConnectBridge(value)
        bridge.start()
        log("Playwright 代理适配: authenticated SOCKS5 -> loopback HTTP CONNECT")
        try:
            yield {"server": bridge.server_url}
        finally:
            bridge.close()
        return

    config = build_playwright_proxy_config(value)
    if config and scheme == "socks5h":
        config["server"] = "socks5://" + config["server"].split("://", 1)[1]
        log("Playwright 代理适配: socks5h -> socks5（无认证）")
    yield config
