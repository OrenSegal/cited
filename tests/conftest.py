"""Shared test setup: import path for the skill's scripts, a local HTTP
fixture server, and a fake DNS resolver. Nothing in the suite touches the
real network."""

from __future__ import annotations

import http.server
import pathlib
import socket
import sys
import threading
from collections import Counter
from contextlib import contextmanager
from typing import Callable, Iterator

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "cited" / "scripts"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))


class Route:
    """One canned response. `body` may be bytes, or a callable taking the
    handler and writing the response itself (for slow-drip / streaming)."""

    def __init__(self, status: int = 200, body: bytes | Callable = b"",
                 headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = {"Content-Type": "text/html; charset=utf-8"} if headers is None else headers


class FixtureServer:
    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.hits: Counter[str] = Counter()
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args) -> None:  # keep test output clean
                pass

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                server.hits[path] += 1
                route = server.routes.get(path)
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return
                if callable(route.body):
                    route.body(self)
                    return
                self.send_response(route.status)
                for key, value in route.headers.items():
                    self.send_header(key, value.replace("{port}", str(server.port)))
                self.send_header("Content-Length", str(len(route.body)))
                self.end_headers()
                try:
                    self.wfile.write(route.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def total_hits(self) -> int:
        return sum(self.hits.values())

    def url(self, path: str = "/", host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"

    def add(self, path: str, status: int = 200, body: bytes | str | Callable = b"",
            headers: dict[str, str] | None = None) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.routes[path] = Route(status, body, headers)

    def start(self) -> FixtureServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def server() -> Iterator[FixtureServer]:
    srv = FixtureServer().start()
    try:
        yield srv
    finally:
        srv.stop()


@contextmanager
def fake_dns(mapping: dict[str, str | list[str]]) -> Iterator[None]:
    """Patch socket.getaddrinfo so the given hostnames resolve to the given
    IPs; everything else resolves normally."""
    real = socket.getaddrinfo

    def resolver(host, port, *args, **kwargs):
        key = host.decode() if isinstance(host, bytes) else host
        if key in mapping:
            ips = mapping[key]
            ips = [ips] if isinstance(ips, str) else ips
            out = []
            for ip in ips:
                family = socket.AF_INET6 if ":" in ip else socket.AF_INET
                sockaddr = (ip, int(port or 0), 0, 0) if family == socket.AF_INET6 else (ip, int(port or 0))
                out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
            return out
        return real(host, port, *args, **kwargs)

    socket.getaddrinfo = resolver
    try:
        yield
    finally:
        socket.getaddrinfo = real
