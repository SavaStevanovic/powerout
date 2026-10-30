import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import timezone
from pathlib import Path

from powerout.agent import HeartbeatSender
from powerout.config import Config, ConfigError, Heartbeat
from powerout.messages import Formatter
from powerout.store import JsonStore
from powerout.watchdog import Registry, WatchdogServer, parse_heartbeat, run_watchdog
from tests.helpers import Clock, FakeDispatcher

TIMEOUT = 180


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.fmt = Formatter(timezone.utc, self.clock)

    def registry(self, store=None):
        return Registry(TIMEOUT, self.fmt, store, self.clock)


class RegistryTest(RegistryTestCase):
    def test_a_new_location_is_registered_silently(self):
        registry = self.registry()
        self.assertIsNone(registry.heartbeat("Stan", "mains", self.clock.now))
        self.assertEqual(registry.snapshot()["Stan"].power, "mains")

    def test_silence_raises_the_alarm_exactly_once(self):
        registry = self.registry()
        start = self.clock.now
        registry.heartbeat("Stan", "mains", start)
        self.assertEqual(registry.check(start + TIMEOUT), [])
        [alarm] = registry.check(start + TIMEOUT + 1)
        self.assertEqual(alarm.title, "⚠️ Nema signala: Stan")
        self.assertEqual(registry.check(start + 10 * TIMEOUT), [])

    def test_a_returning_signal_clears_the_alarm_and_reports_the_silence(self):
        registry = self.registry()
        start = self.clock.now
        registry.heartbeat("Stan", "mains", start)
        registry.check(start + TIMEOUT + 1)
        self.clock.now = start + 600
        message = registry.heartbeat("Stan", "mains", start + 600)
        self.assertEqual(message.title, "✅ Signal je ponovo tu: Stan")
        self.assertIn("10 min", message.body)
        self.assertIsNone(registry.heartbeat("Stan", "mains", start + 660))
        self.assertEqual(registry.check(start + 700), [])
        self.assertEqual(len(registry.check(start + 660 + TIMEOUT + 1)), 1)  # and it can alarm again

    def test_the_alarm_blames_the_power_only_when_the_device_was_on_battery(self):
        registry = self.registry()
        registry.heartbeat("A", "battery", self.clock.now)
        registry.heartbeat("B", "mains", self.clock.now)
        alarms = {m.title: m.body for m in registry.check(self.clock.now + TIMEOUT + 1)}
        self.assertIn("verovatno nestala struja", alarms["⚠️ Nema signala: A"])
        self.assertNotIn("verovatno nestala struja", alarms["⚠️ Nema signala: B"])

    def test_locations_are_tracked_separately(self):
        registry = self.registry()
        start = self.clock.now
        registry.heartbeat("A", "mains", start)
        registry.heartbeat("B", "mains", start)
        registry.heartbeat("B", "mains", start + TIMEOUT)
        [alarm] = registry.check(start + TIMEOUT + 1)
        self.assertIn("A", alarm.title)


class PersistenceTest(RegistryTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = JsonStore(Path(tmp.name) / "watchdog.json")

    def test_an_alarm_survives_a_restart_and_is_cleared_by_the_next_heartbeat(self):
        before = self.registry(self.store)
        start = self.clock.now
        before.heartbeat("Stan", "battery", start)
        before.check(start + TIMEOUT + 1)

        self.clock.now = start + 3600
        after = self.registry(self.store)
        self.assertEqual(after.check(self.clock.now + 1000), [])  # already alarmed; not repeated
        message = after.heartbeat("Stan", "mains", self.clock.now)
        self.assertIn("1 h", message.body)  # counted from the last real heartbeat

    def test_agents_get_a_full_timeout_after_the_watchdog_itself_was_down(self):
        start = self.clock.now
        self.registry(self.store).heartbeat("Stan", "mains", start)

        self.clock.now = start + 3600  # the watchdog was off for an hour
        after = self.registry(self.store)
        self.assertEqual(after.check(self.clock.now + TIMEOUT), [])
        self.assertEqual(len(after.check(self.clock.now + TIMEOUT + 1)), 1)

    def test_a_damaged_state_file_starts_empty(self):
        self.store.path.write_text("garbage")
        with self.assertLogs("powerout.store", "WARNING"):
            registry = self.registry(self.store)
        self.assertEqual(registry.snapshot(), {})

    def test_damaged_entries_are_skipped(self):
        self.store.save(
            {"peers": {"good": {"last_seen": self.clock.now}, "bad": {"power": "mains"}, "worse": "x", "ugly": None}}
        )
        self.assertEqual(list(self.registry(self.store).snapshot()), ["good"])

    def test_changes_are_written_to_disk(self):
        registry = self.registry(self.store)
        registry.heartbeat("Stan", "battery", self.clock.now)
        saved = self.store.load()["peers"]["Stan"]
        self.assertEqual((saved["power"], saved["down"]), ("battery", False))
        registry.check(self.clock.now + TIMEOUT + 1)
        self.assertTrue(self.store.load()["peers"]["Stan"]["down"])


class ParseHeartbeatTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_heartbeat(b'{"name": " Stan ", "power": "battery"}'), ("Stan", "battery"))

    def test_unknown_or_missing_power_is_unknown(self):
        self.assertEqual(parse_heartbeat(b'{"name": "Stan", "power": "solar"}'), ("Stan", "unknown"))
        self.assertEqual(parse_heartbeat(b'{"name": "Stan"}'), ("Stan", "unknown"))

    def test_malformed_bodies(self):
        bodies = [b"", b"nope", b"[]", b'"Stan"', b'{"power": "mains"}', b'{"name": ""}', b'{"name": "  "}']
        bodies += [b'{"name": 5}', b'{"name": "' + b"x" * 65 + b'"}', b"\xff\xfe"]
        bodies += [b'{"name": "Stan\\nBcc: x@y.z"}', b'{"name": "Stan\\u0000"}']  # would end up in message headers
        for body in bodies:
            with self.subTest(body=body[:20]):
                with self.assertRaises(ValueError):
                    parse_heartbeat(body)


TOKEN = "t" * 20


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.dispatcher = FakeDispatcher()
        self.registry = Registry(TIMEOUT, Formatter(timezone.utc))
        self.server = WatchdogServer(("127.0.0.1", 0), self.registry, self.dispatcher, TOKEN)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.shutdown)
        self.port = self.server.server_address[1]

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def post(self, body, *, token=TOKEN, path="/heartbeat", authorization=None):
        headers = {"Content-Type": "application/json"}
        if authorization or token:
            headers["Authorization"] = authorization or f"Bearer {token}"
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as e:
            with e:
                return e.code

    def raw(self, request: str) -> str:
        """Send a hand-written request; returns the status line."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(request.encode())
            return s.recv(4096).split(b"\r\n")[0].decode()


class ServerTest(ServerTestCase):
    def test_a_valid_heartbeat_is_recorded(self):
        self.assertEqual(self.post({"name": "Stan", "power": "battery"}), 204)
        self.assertEqual(self.registry.snapshot()["Stan"].power, "battery")

    def test_the_token_is_required(self):
        for kwargs in [{"token": ""}, {"token": "wrong"}, {"authorization": "Basic " + TOKEN}, {"authorization": TOKEN}]:
            with self.subTest(**kwargs):
                self.assertEqual(self.post({"name": "Stan"}, **kwargs), 401)
        self.assertEqual(self.registry.snapshot(), {})

    def test_the_bearer_scheme_is_case_insensitive(self):
        self.assertEqual(self.post({"name": "Stan"}, authorization=f"bearer {TOKEN}"), 204)

    def test_malformed_bodies_are_rejected(self):
        for body in [b"nope", b"[]", {"power": "mains"}, {"name": ""}, {"name": "x" * 65}]:
            with self.subTest(body=body):
                self.assertEqual(self.post(body), 400)
        self.assertEqual(self.registry.snapshot(), {})

    def test_unknown_paths(self):
        self.assertEqual(self.post({"name": "Stan"}, path="/other"), 404)
        self.assertIn(" 404 ", self.raw("GET /heartbeat HTTP/1.0\r\n\r\n"))  # heartbeats are POST-only
        self.assertIn(" 404 ", self.raw("GET / HTTP/1.0\r\n\r\n"))

    def test_oversized_bodies_are_refused_before_being_read(self):
        status = self.raw(
            f"POST /heartbeat HTTP/1.0\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: 99999\r\n\r\n"
        )
        self.assertIn(" 413 ", status)

    def test_a_missing_length_is_refused(self):
        status = self.raw(f"POST /heartbeat HTTP/1.0\r\nAuthorization: Bearer {TOKEN}\r\n\r\n")
        self.assertIn(" 411 ", status)

    def test_a_nonsensical_length_is_a_bad_request(self):
        for length in ["abc", "-5", "1.5"]:
            with self.subTest(length=length):
                status = self.raw(
                    f"POST /heartbeat HTTP/1.0\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: {length}\r\n\r\n"
                )
                self.assertIn(" 400 ", status)

    def test_an_empty_body_is_a_bad_request_not_an_oversized_one(self):
        self.assertEqual(self.post(b""), 400)

    def test_wrong_token_is_checked_before_the_body_length(self):
        status = self.raw("POST /heartbeat HTTP/1.0\r\nAuthorization: Bearer nope\r\nContent-Length: 99999\r\n\r\n")
        self.assertIn(" 401 ", status)

    def test_health_needs_no_token(self):
        with urllib.request.urlopen(self.url + "/health", timeout=5) as response:
            self.assertEqual((response.status, response.read()), (200, b"ok\n"))

    def test_the_server_does_not_announce_its_python_version(self):
        with urllib.request.urlopen(self.url + "/health", timeout=5) as response:
            self.assertEqual(response.headers["Server"], "powerout")

    def test_the_return_of_a_silent_location_is_announced(self):
        last_heard = time.time() - 600  # the server stamps requests with the real clock
        self.registry.heartbeat("Stan", "mains", last_heard)
        self.registry.check(last_heard + TIMEOUT + 1)
        self.assertEqual(self.post({"name": "Stan"}), 204)
        self.assertEqual(self.dispatcher.titles, ["✅ Signal je ponovo tu: Stan"])
        self.assertIn("10 min", self.dispatcher.messages[0].body)

    def test_an_ordinary_heartbeat_announces_nothing(self):
        self.post({"name": "Stan"})
        self.post({"name": "Stan"})
        self.assertEqual(self.dispatcher.messages, [])


class AgentToWatchdogTest(ServerTestCase):
    def sender(self, token=TOKEN, power="battery"):
        cfg = Heartbeat(url=self.url + "/heartbeat", token=token)
        return HeartbeatSender(cfg, "Stan", lambda: power)

    def test_the_agents_heartbeat_is_accepted(self):
        self.assertTrue(self.sender().beat())
        self.assertEqual(self.registry.snapshot()["Stan"].power, "battery")

    def test_a_wrong_token_makes_the_beat_fail_loudly(self):
        with self.assertLogs("powerout.agent", "WARNING") as logs:
            self.assertFalse(self.sender(token="wrong-token").beat())
        self.assertIn("401", logs.output[0])


class RunWatchdogTest(unittest.TestCase):
    def test_a_missing_or_short_token_is_refused(self):
        for token in ["", "short"]:
            with self.subTest(token=token):
                cfg = Config.from_dict({"watchdog": {"token": token}, "telegram": {"token": "t", "chat_id": "1"}})
                with self.assertRaises(ConfigError) as caught:
                    run_watchdog(cfg, threading.Event())
                self.assertIn("[watchdog] token", str(caught.exception))

    def test_a_port_that_is_taken_is_reported_clearly(self):
        with socket.socket() as taken, tempfile.TemporaryDirectory() as state_dir:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            cfg = Config.from_dict(
                {
                    "watchdog": {"token": TOKEN, "port": port},
                    "telegram": {"token": "t", "chat_id": "1"},
                    "general": {"state_dir": state_dir},
                }
            )
            with self.assertRaises(ConfigError) as caught:
                run_watchdog(cfg, threading.Event())
        self.assertIn(str(port), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
