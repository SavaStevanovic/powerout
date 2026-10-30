"""Test helpers: a stub HTTP server, a fake dispatcher, a fake clock."""

import json
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple


class Request(NamedTuple):
    method: str
    path: str
    headers: dict
    body: bytes

    def json(self):
        return json.loads(self.body)


class StubServer:
    """A local HTTP server that records requests and answers with a canned or computed (status, json)."""

    def __init__(self, respond=None):
        self.requests: list[Request] = []
        self.respond = respond or (lambda request: (200, {"ok": True, "result": {}}))
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def handle_any(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                request = Request(self.command, self.path, dict(self.headers), body)
                stub.requests.append(request)
                status, payload = stub.respond(request)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = handle_any

            def log_message(self, *args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()


class FakeDispatcher:
    """Collects submitted messages instead of delivering them."""

    def __init__(self):
        self.messages = []
        self.waiting = 0

    def submit(self, message):
        self.messages.append(message)

    def pending(self):
        return self.waiting

    @property
    def titles(self):
        return [m.title for m in self.messages]


class Clock:
    """A clock that only moves when told to; callable like time.time()."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def fixture(name: str) -> bytes:
    """A page saved from the distributor's site (see tests/fixtures)."""
    return (Path(__file__).parent / "fixtures" / name).read_bytes()
