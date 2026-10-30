import os
import re
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from powerout import config
from powerout.config import Config, ConfigError

ROOT = Path(__file__).resolve().parent.parent


def parse(text: str) -> Config:
    return Config.from_dict(tomllib.loads(text))


class ParseTest(unittest.TestCase):
    def test_empty_config_has_working_defaults(self):
        cfg = Config.from_dict({})
        self.assertIsNone(cfg.telegram)
        self.assertIsNone(cfg.email)
        self.assertEqual(cfg.power.source, "auto")
        self.assertEqual(cfg.general.delivery, "all")
        self.assertTrue(cfg.general.name)  # falls back to the hostname
        self.assertEqual(cfg.watchdog.host, "127.0.0.1")

    def test_full_example(self):
        cfg = parse(
            """
            [general]
            name = "Stan"
            timezone = "Europe/Belgrade"
            delivery = "fallback"
            [telegram]
            token = "123:abc"
            chat_id = "42"
            [email]
            host = "smtp.example.com"
            username = "me@example.com"
            password = "secret"
            to = ["me@example.com"]
            [power]
            source = "nut"
            ups = "myups@localhost"
            debounce_seconds = 5
            [heartbeat]
            url = "https://watch.example.com/heartbeat"
            interval_seconds = 30
            [watchdog]
            port = 9000
            token = "x"
            """
        )
        self.assertEqual(cfg.general.name, "Stan")
        self.assertEqual(str(cfg.general.tz), "Europe/Belgrade")
        self.assertEqual(cfg.telegram.chat_id, ["42"])
        self.assertEqual(cfg.email.sender, "me@example.com")
        self.assertEqual(cfg.power.debounce_seconds, 5.0)
        self.assertEqual(cfg.heartbeat.interval_seconds, 30.0)
        self.assertEqual(cfg.watchdog.port, 9000)

    def test_secrets_stay_out_of_reprs(self):
        cfg = parse('[telegram]\ntoken = "SECRET-TOKEN"\n[email]\nhost="h"\npassword="SECRET-PASS"\nsender="a@b"\nto=["a@b"]')
        self.assertNotIn("SECRET", repr(cfg))

    def test_chat_id_may_be_a_number_a_string_or_a_list(self):
        for value, expected in [("123", ["123"]), (123, ["123"]), ([1, "-2"], ["1", "-2"])]:
            with self.subTest(value=value):
                cfg = Config.from_dict({"telegram": {"token": "t", "chat_id": value}})
                self.assertEqual(cfg.telegram.chat_id, expected)

    def test_telegram_without_chat_id_loads(self):
        # needed so that `telegram-chat-id` can run before the id is known
        self.assertEqual(Config.from_dict({"telegram": {"token": "t"}}).telegram.chat_id, [])

    def test_email_ports_follow_the_security_mode(self):
        base = {"host": "h", "sender": "a@b", "to": ["a@b"]}
        for security, port in [("ssl", 465), ("starttls", 587), ("none", 25)]:
            with self.subTest(security=security):
                self.assertEqual(Config.from_dict({"email": {**base, "security": security}}).email.port, port)
        self.assertEqual(Config.from_dict({"email": {**base, "port": 2525}}).email.port, 2525)


class ValidationTest(unittest.TestCase):
    def assertRejected(self, raw: dict, *fragments: str):
        with self.assertRaises(ConfigError) as caught:
            Config.from_dict(raw)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    def test_unknown_section(self):
        self.assertRejected({"telegrm": {}}, "[telegrm]")

    def test_unknown_option_names_the_typo(self):
        self.assertRejected({"power": {"debounse_seconds": 5}}, "[power]", "debounse_seconds")

    def test_section_must_be_a_table(self):
        self.assertRejected({"power": 5}, "[power]")

    def test_wrong_types(self):
        self.assertRejected({"power": {"poll_seconds": "fast"}}, "[power] poll_seconds")
        self.assertRejected({"power": {"poll_seconds": True}}, "[power] poll_seconds")
        self.assertRejected({"watchdog": {"port": 80.5}}, "[watchdog] port")
        self.assertRejected({"general": {"name": 5}}, "[general] name")
        self.assertRejected({"email": {"host": "h", "to": [["a"]]}}, "[email] to")

    def test_enumerations(self):
        self.assertRejected({"general": {"delivery": "both"}}, '"all"', '"fallback"')
        self.assertRejected({"power": {"source": "magic"}}, "[power] source")
        self.assertRejected({"email": {"host": "h", "to": ["a"], "sender": "a", "security": "tls"}}, "[email] security")

    def test_ranges(self):
        self.assertRejected({"watchdog": {"port": 0}}, "[watchdog] port")
        self.assertRejected({"watchdog": {"timeout_seconds": 1}}, "timeout_seconds")
        self.assertRejected({"heartbeat": {"interval_seconds": 0}}, "interval_seconds")
        self.assertRejected({"power": {"debounce_seconds": -1}}, "debounce_seconds")

    def test_required_fields(self):
        self.assertRejected({"telegram": {}}, "[telegram] token")
        self.assertRejected({"email": {"to": ["a@b"]}}, "[email] host")
        self.assertRejected({"email": {"host": "h"}}, "[email] to")
        self.assertRejected({"email": {"host": "h", "to": ["a@b"]}}, "[email] sender")
        self.assertRejected({"power": {"source": "nut"}}, "[power] ups")
        self.assertRejected({"power": {"source": "command"}}, "[power] command")

    def test_heartbeat_url_needs_a_scheme(self):
        self.assertRejected({"heartbeat": {"url": "example.com/heartbeat"}}, "http")

    def test_telegram_api_base_needs_a_scheme(self):
        self.assertRejected({"telegram": {"token": "t", "api_base": "api.telegram.org"}}, "[telegram] api_base")

    def test_pasted_whitespace_around_secrets_is_stripped(self):
        cfg = Config.from_dict(
            {
                "telegram": {"token": " 123:abc\n"},
                "heartbeat": {"url": "https://x.example/ping/abc ", "token": "\tsecret "},
                "watchdog": {"token": "secret\n"},
            }
        )
        self.assertEqual(cfg.telegram.token, "123:abc")
        self.assertEqual(cfg.heartbeat.url, "https://x.example/ping/abc")
        self.assertEqual(cfg.heartbeat.token, "secret")
        self.assertEqual(cfg.watchdog.token, "secret")

    def test_whitespace_or_control_characters_inside_a_secret_are_rejected(self):
        self.assertRejected({"telegram": {"token": "123:ab cd"}}, "[telegram] token", "razmak")
        self.assertRejected({"telegram": {"token": "123:ab\x00cd"}}, "[telegram] token")
        self.assertRejected({"heartbeat": {"url": "https://x.example/a b"}}, "[heartbeat] url")
        self.assertRejected({"heartbeat": {"url": "https://x.example", "token": "a\nb"}}, "[heartbeat] token")
        self.assertRejected({"watchdog": {"token": "a b"}}, "[watchdog] token")

    def test_a_token_of_only_whitespace_counts_as_missing(self):
        self.assertRejected({"telegram": {"token": "  "}}, "[telegram] token", "obavezno")

    def test_timezone(self):
        self.assertRejected({"general": {"timezone": "Mars/Olympus"}}, "Mars/Olympus")

    def test_the_location_name_cannot_break_message_headers(self):
        # it ends up in e-mail subjects; a line break there once made a message undeliverable forever
        self.assertRejected({"general": {"name": "Stan\nBcc: x@y.z"}}, "[general] name")
        self.assertRejected({"general": {"name": "Stan\x00"}}, "[general] name")
        self.assertRejected({"general": {"name": "x" * 65}}, "[general] name")
        self.assertEqual(Config.from_dict({"general": {"name": "Vikendica 🏡"}}).general.name, "Vikendica 🏡")


class StatePathTest(unittest.TestCase):
    def test_explicit_directory_wins(self):
        cfg = Config.from_dict({"general": {"state_dir": "/srv/powerout"}})
        self.assertEqual(cfg.general.state_path, Path("/srv/powerout"))

    def test_systemd_state_directory(self):
        with mock.patch.dict(os.environ, {"STATE_DIRECTORY": "/var/lib/powerout"}):
            self.assertEqual(Config.from_dict({}).general.state_path, Path("/var/lib/powerout"))

    def test_xdg_default(self):
        env = {"XDG_STATE_HOME": "/home/x/.state"}
        with mock.patch.dict(os.environ, env), mock.patch.dict(os.environ, clear=False):
            os.environ.pop("STATE_DIRECTORY", None)
            self.assertEqual(Config.from_dict({}).general.state_path, Path("/home/x/.state/powerout"))


class ShippedFilesTest(unittest.TestCase):
    """The example config and the service units are documentation people copy; keep them in step with the code."""

    def example(self) -> str:
        return (ROOT / "config.example.toml").read_text(encoding="utf-8")

    def test_the_example_config_is_valid(self):
        cfg = parse(self.example())
        self.assertEqual(cfg.telegram.chat_id, ["123456789"])
        self.assertIsNone(cfg.email)

    def test_every_commented_out_option_in_the_example_is_valid_too(self):
        cfg = parse(re.sub(r"^#(?=\[|\w+ = )", "", self.example(), flags=re.M))
        self.assertEqual(cfg.email.host, "smtp.gmail.com")
        self.assertEqual(cfg.heartbeat.url, "https://watchdog.example.com/heartbeat")
        self.assertEqual(cfg.power.ups, "myups@localhost")

    def test_the_service_units_match_what_the_program_expects(self):
        state_dirs = set()
        for command in ("agent", "watchdog"):
            text = (ROOT / "systemd" / f"powerout-{command}.service").read_text(encoding="utf-8")
            self.assertRegex(text, rf"(?m)^ExecStart=/opt/powerout/bin/powerout {command}$")
            # the program finds it as $CREDENTIALS_DIRECTORY/config.toml
            self.assertIn("LoadCredential=config.toml:/etc/powerout/config.toml", text)
            state_dirs.add(re.search(r"(?m)^StateDirectory=(\S+)$", text).group(1))
        self.assertEqual(len(state_dirs), 2)  # two dynamic users on one machine must not share a directory


class LoadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, text: str, mode: int = 0o600) -> Path:
        path = self.dir / "config.toml"
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_load_reads_the_file_and_remembers_the_path(self):
        path = self.write('[general]\nname = "Stan"')
        cfg = config.load(path)
        self.assertEqual(cfg.general.name, "Stan")
        self.assertEqual(cfg.path, path)

    def test_missing_file(self):
        with self.assertRaises(ConfigError) as caught:
            config.load(self.dir / "nope.toml")
        self.assertIn("nope.toml", str(caught.exception))

    def test_syntax_error_names_the_file(self):
        path = self.write("[general\nname = ")
        with self.assertRaises(ConfigError) as caught:
            config.load(path)
        self.assertIn("config.toml", str(caught.exception))

    def test_warns_when_readable_by_others(self):
        path = self.write("", mode=0o644)
        with self.assertLogs("powerout.config", "WARNING") as logs:
            config.load(path)
        self.assertIn("chmod 600", logs.output[0])

    def test_silent_when_private(self):
        path = self.write("", mode=0o600)
        with self.assertNoLogs("powerout.config", "WARNING"):
            config.load(path)

    def test_find_prefers_the_explicit_path(self):
        self.assertEqual(config.find("/some/where.toml"), Path("/some/where.toml"))

    def test_find_takes_the_first_existing_default(self):
        path = self.write("")
        with mock.patch.object(config, "default_paths", return_value=[self.dir / "missing.toml", path]):
            self.assertEqual(config.find(), path)

    def test_find_looks_in_the_systemd_credentials_directory(self):
        with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": "/run/credentials/powerout-agent.service"}):
            self.assertIn(Path("/run/credentials/powerout-agent.service/config.toml"), config.default_paths())

    def test_find_returns_none_when_nothing_exists(self):
        with mock.patch.object(config, "default_paths", return_value=[self.dir / "missing.toml"]):
            self.assertIsNone(config.find())


if __name__ == "__main__":
    unittest.main()
