import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from powerout import config
from powerout.cli import main
from tests.helpers import StubServer


class CliTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # never pick up a real config.toml from the machine running the tests
        patch = mock.patch.object(config, "default_paths", return_value=[])
        patch.start()
        self.addCleanup(patch.stop)

    def config(self, text: str) -> str:
        path = self.dir / "config.toml"
        path.write_text(text)
        path.chmod(0o600)
        return str(path)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()


class TestCommandTest(CliTestCase):
    def telegram_config(self, server):
        return self.config(
            f'[general]\nname = "Stan"\n[telegram]\ntoken = "123:abc"\nchat_id = "42"\napi_base = "{server.url}"\n'
        )

    def test_sends_a_test_message_and_reports_each_channel(self):
        with StubServer() as server:
            code, out, _ = self.run_cli("-c", self.telegram_config(server), "test")
        self.assertEqual(code, 0)
        self.assertIn("telegram: OK", out)
        self.assertIn("Probna poruka: Stan", server.requests[0].json()["text"])

    def test_reports_a_failing_channel_and_exits_nonzero(self):
        with StubServer(lambda r: (401, {"ok": False, "description": "Unauthorized"})) as server:
            code, out, _ = self.run_cli("-c", self.telegram_config(server), "test")
        self.assertEqual(code, 1)
        self.assertIn("GREŠKA", out)
        self.assertIn("Unauthorized", out)
        self.assertNotIn("telegram: GREŠKA - telegram:", out)  # the channel is named once

    def test_fallback_delivery_does_not_stop_the_test_from_trying_every_channel(self):
        text = (
            '[general]\ndelivery = "fallback"\n[telegram]\ntoken = "t"\nchat_id = "1"\napi_base = "{url}"\n'
            '[email]\nhost = "127.0.0.1"\nport = 1\nsecurity = "none"\nsender = "a@b"\nto = ["a@b"]\n'
        )
        with StubServer() as server:
            code, out, _ = self.run_cli("-c", self.config(text.replace("{url}", server.url)), "test")
        self.assertIn("email:", out)
        self.assertIn("telegram: OK", out)
        self.assertEqual(code, 1)  # the e-mail channel points at a closed port

    def test_without_any_channel_it_explains_what_to_add(self):
        code, _, err = self.run_cli("-c", self.config(""), "test")
        self.assertEqual(code, 2)
        self.assertIn("[telegram]", err)


class ConfigErrorsTest(CliTestCase):
    def test_no_config_file_tells_how_to_create_one(self):
        code, _, err = self.run_cli("test")
        self.assertEqual(code, 2)
        self.assertIn("config.example.toml", err)

    def test_explicit_config_that_does_not_exist(self):
        code, _, err = self.run_cli("-c", str(self.dir / "nope.toml"), "test")
        self.assertEqual(code, 2)
        self.assertIn("nope.toml", err)

    def test_a_typo_in_the_config_is_named(self):
        code, _, err = self.run_cli("-c", self.config("[power]\ndebounse_seconds = 5\n"), "agent")
        self.assertEqual(code, 2)
        self.assertIn("debounse_seconds", err)

    def test_the_watchdog_refuses_to_start_without_a_token(self):
        code, _, err = self.run_cli("-c", self.config('[telegram]\ntoken = "t"\nchat_id = "1"\n'), "watchdog")
        self.assertEqual(code, 2)
        self.assertIn("[watchdog] token", err)


class TelegramChatIdTest(CliTestCase):
    def run_with_updates(self, updates):
        with StubServer(lambda r: (200, {"ok": True, "result": updates})) as server:
            cfg = self.config(f'[telegram]\ntoken = "123:abc"\napi_base = "{server.url}"\n')
            return self.run_cli("-c", cfg, "telegram-chat-id")

    def test_prints_the_chat_id_ready_to_paste(self):
        updates = [{"update_id": 1, "message": {"chat": {"id": 42, "first_name": "Sava"}}}]
        code, out, _ = self.run_with_updates(updates)
        self.assertEqual(code, 0)
        self.assertIn('chat_id = "42"', out)

    def test_explains_what_to_do_when_the_bot_has_heard_nothing(self):
        code, out, _ = self.run_with_updates([])
        self.assertEqual(code, 1)
        self.assertIn("/start", out)

    def test_needs_a_telegram_section(self):
        code, _, err = self.run_cli("-c", self.config(""), "telegram-chat-id")
        self.assertEqual(code, 2)
        self.assertIn("[telegram]", err)

    def test_api_failure_is_reported_without_a_traceback(self):
        with StubServer(lambda r: (401, {"ok": False, "description": "Unauthorized"})) as server:
            cfg = self.config(f'[telegram]\ntoken = "123:abc"\napi_base = "{server.url}"\n')
            code, _, err = self.run_cli("-c", cfg, "telegram-chat-id")
        self.assertEqual(code, 1)
        self.assertIn("Unauthorized", err)
        self.assertNotIn("123:abc", err)


class CheckPowerTest(CliTestCase):
    def test_reports_mains(self):
        code, out, _ = self.run_cli("-c", self.config('[power]\nsource = "command"\ncommand = "true"\n'), "check-power")
        self.assertEqual(code, 0)
        self.assertIn("mrežno napajanje", out)

    def test_reports_battery(self):
        code, out, _ = self.run_cli("-c", self.config('[power]\nsource = "command"\ncommand = "false"\n'), "check-power")
        self.assertEqual(code, 0)
        self.assertIn("baterija", out)

    def test_reports_unknown(self):
        cfg = self.config('[power]\nsource = "command"\ncommand = "/nonexistent/check"\n')
        self.assertIn("nepoznato", self.run_cli("-c", cfg, "check-power")[1])

    def test_a_machine_with_nothing_to_read_is_told_what_to_do(self):
        code, out, _ = self.run_cli("-c", self.config('[power]\nsource = "none"\n'), "check-power")
        self.assertEqual(code, 1)
        self.assertIn("watchdog", out)

    def test_works_without_any_config_file(self):
        with mock.patch("powerout.cli.detect_source", return_value=None):
            code, out, _ = self.run_cli("check-power")
        self.assertEqual(code, 1)
        self.assertIn("watchdog", out)


class ParserTest(CliTestCase):
    def test_help_lists_the_commands(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        for command in ["agent", "watchdog", "site", "test", "check-power", "telegram-chat-id"]:
            self.assertIn(command, out.getvalue())

    def test_a_command_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main([])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
