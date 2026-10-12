"""UrllibHttpClient against a local stdlib HTTP server (loopback only, no internet)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from governed_agent_engine.cognition.http import HttpTransportError, UrllibHttpClient


class _H(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()
            return
        code = 429 if self.path == "/limited" else 200
        out = json.dumps({"echo": body, "ua": self.headers["User-Agent"]}).encode()
        self.send_response(code)
        self.send_header("Retry-After", "3")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def server() -> Iterator[str]:
    srv = HTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_roundtrip(server: str) -> None:
    c = UrllibHttpClient()
    r = c.request("POST", f"{server}/ok", headers={}, json_body={"a": 1})
    assert r.status == 200
    assert r.json()["echo"] == {"a": 1}
    assert r.json()["ua"].startswith("governed-agent-engine/")
    r = c.request("POST", f"{server}/limited", headers={}, json_body={})
    assert r.status == 429
    assert r.headers["retry-after"] == "3"
    assert "echo" in r.text()


def test_redirect_not_followed(server: str) -> None:
    r = UrllibHttpClient().request("POST", f"{server}/redirect", headers={}, json_body={})
    assert r.status == 302


def test_refuses_plain_http_remote() -> None:
    with pytest.raises(HttpTransportError, match="non-https"):
        UrllibHttpClient().request("GET", "http://example.com", headers={})


def test_connection_error() -> None:
    with pytest.raises(HttpTransportError):
        UrllibHttpClient().request("GET", "http://127.0.0.1:1/x", headers={}, timeout=2)
