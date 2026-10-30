"""The watchdog: runs somewhere that keeps its power, and raises the alarm when an agent goes silent."""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import MAX_NAME, Config, ConfigError
from .dispatch import Dispatcher
from .messages import Formatter
from .notify import Message, build_notifier
from .store import JsonStore

log = logging.getLogger(__name__)

MAX_BODY = 4096
MIN_TOKEN = 16
POWER_STATES = ("mains", "battery", "unknown")


@dataclass
class Peer:
    """One monitored location."""

    last_seen: float
    power: str = "unknown"
    down: bool = False  # the alarm has been raised and not yet cleared


class Registry:
    """Who reported when. Knows nothing about HTTP or threads except that it may be called from several."""

    def __init__(
        self,
        timeout: float,
        formatter: Formatter,
        store: JsonStore | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._timeout = timeout
        self._formatter = formatter
        self._store = store
        self._lock = threading.Lock()
        self._peers: dict[str, Peer] = {}
        now = clock()
        for name, entry in ((store.load() if store else None) or {}).get("peers", {}).items():
            try:
                peer = Peer(float(entry["last_seen"]), str(entry.get("power", "unknown")), bool(entry.get("down")))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if not peer.down:
                # The watchdog may have been down itself; give every agent a full timeout to check in.
                peer.last_seen = max(peer.last_seen, now)
            self._peers[name] = peer

    def snapshot(self) -> dict[str, Peer]:
        with self._lock:
            return {name: Peer(p.last_seen, p.power, p.down) for name, p in self._peers.items()}

    def heartbeat(self, name: str, power: str, now: float) -> Message | None:
        """Record a heartbeat; returns the "signal is back" message if the alarm was on."""
        with self._lock:
            peer = self._peers.get(name)
            message = None
            if peer is None:
                log.info("new location: %r", name)
                self._peers[name] = Peer(now, power)
            else:
                if peer.down:
                    message = self._formatter.signal_restored(name, peer.last_seen, now)
                    log.warning("%r is back after %.0f s", name, now - peer.last_seen)
                peer.last_seen, peer.power, peer.down = now, power, False
            self._save()
        return message

    def check(self, now: float) -> list[Message]:
        """Raise the alarm for every location that has been silent for too long (once per silence)."""
        messages = []
        with self._lock:
            for name, peer in self._peers.items():
                if not peer.down and now - peer.last_seen > self._timeout:
                    peer.down = True
                    log.warning("no signal from %r for %.0f s", name, now - peer.last_seen)
                    messages.append(self._formatter.signal_lost(name, peer.last_seen, peer.power, now))
            if messages:
                self._save()
        return messages

    def _save(self) -> None:
        if self._store:
            self._store.save(
                {
                    "peers": {
                        name: {"last_seen": p.last_seen, "power": p.power, "down": p.down}
                        for name, p in self._peers.items()
                    }
                }
            )


def parse_heartbeat(body: bytes) -> tuple[str, str]:
    """(name, power) from a heartbeat request body; ValueError if it is malformed."""
    data = json.loads(body)
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not 0 < len(name.strip()) <= MAX_NAME or not name.isprintable():
        # printable: the name goes into message titles and e-mail headers
        raise ValueError(f"name must be 1-{MAX_NAME} printable characters")
    power = data.get("power")
    return name.strip(), power if power in POWER_STATES else "unknown"


class _Handler(BaseHTTPRequestHandler):
    server: WatchdogServer
    timeout = 10  # drop clients that stall

    def version_string(self) -> str:
        return "powerout"  # not the Python version

    def do_GET(self) -> None:
        if self.path != "/health":
            return self.send_error(404)
        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/heartbeat":
            return self.send_error(404)
        if not self._authorized():
            log.warning("rejected a request from %s: wrong or missing token", self.client_address[0])
            return self.send_error(401)
        declared = self.headers.get("Content-Length")
        if declared is None:
            return self.send_error(411)
        try:
            length = int(declared)
        except ValueError:
            return self.send_error(400)
        if length < 0:
            return self.send_error(400)
        if length > MAX_BODY:
            return self.send_error(413)
        try:
            name, power = parse_heartbeat(self.rfile.read(length))
        except ValueError:
            return self.send_error(400)
        message = self.server.registry.heartbeat(name, power, time.time())
        if message:
            self.server.dispatcher.submit(message)
        self.send_response(204)
        self.end_headers()

    def _authorized(self) -> bool:
        scheme, _, supplied = self.headers.get("Authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(supplied.strip().encode(), self.server.token)

    def log_message(self, format: str, *args) -> None:
        log.debug("%s %s", self.client_address[0], format % args)


class WatchdogServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], registry: Registry, dispatcher: Dispatcher, token: str):
        super().__init__(address, _Handler)
        self.registry = registry
        self.dispatcher = dispatcher
        self.token = token.encode()


def run_watchdog(cfg: Config, stop: threading.Event) -> None:
    wd = cfg.watchdog
    if len(wd.token) < MIN_TOKEN:
        raise ConfigError(
            f"[watchdog] token: obavezan, najmanje {MIN_TOKEN} znakova; napravi ga sa: "
            "python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    formatter = Formatter(cfg.general.tz)
    dispatcher = Dispatcher(build_notifier(cfg), decorate=formatter.delayed)
    registry = Registry(wd.timeout_seconds, formatter, JsonStore(cfg.general.state_path / "watchdog.json"))
    try:
        server = WatchdogServer((wd.host, wd.port), registry, dispatcher, wd.token)
    except OSError as e:
        raise ConfigError(f"ne mogu da otvorim {wd.host}:{wd.port}: {e.strerror}") from None
    log.info("watchdog listening on %s:%d, alarm after %.0f s of silence", wd.host, wd.port, wd.timeout_seconds)
    dispatcher.start()
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    try:
        while not stop.wait(min(10.0, wd.timeout_seconds / 3)):
            try:
                for message in registry.check(time.time()):
                    dispatcher.submit(message)
            except Exception:
                log.exception("check failed")
    finally:
        server.shutdown()
        server.server_close()
        dispatcher.stop()
