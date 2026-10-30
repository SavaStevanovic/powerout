import smtplib
import socket
import unittest
from unittest import mock

from powerout.config import Config, ConfigError, Email
from powerout.notify import (
    EmailChannel,
    Message,
    NotifyError,
    Notifier,
    TelegramChannel,
    build_notifier,
)
from tests.helpers import StubServer

TOKEN = "123456:SECRET-TOKEN"
MESSAGE = Message("⚡ Nestala struja: Stan", "Struja je nestala u 14:03:12.")


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TelegramTest(unittest.TestCase):
    def test_sends_title_and_body_to_the_chat(self):
        with StubServer() as server:
            TelegramChannel(TOKEN, ["42"], server.url).send(MESSAGE)
        [request] = server.requests
        self.assertEqual((request.method, request.path), ("POST", f"/bot{TOKEN}/sendMessage"))
        self.assertEqual(request.json(), {"chat_id": "42", "text": f"{MESSAGE.title}\n\n{MESSAGE.body}"})

    def test_sends_to_every_chat(self):
        with StubServer() as server:
            TelegramChannel(TOKEN, ["1", "2"], server.url).send(MESSAGE)
        self.assertEqual([r.json()["chat_id"] for r in server.requests], ["1", "2"])

    def test_api_errors_carry_the_description_but_not_the_token(self):
        body = {"ok": False, "error_code": 401, "description": "Unauthorized"}
        with StubServer(lambda r: (401, body)) as server:
            with self.assertRaises(NotifyError) as caught:
                TelegramChannel(TOKEN, ["42"], server.url).send(MESSAGE)
        self.assertIn("Unauthorized", str(caught.exception))
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("SECRET-TOKEN", str(caught.exception))

    def test_the_usual_setup_mistakes_come_with_a_hint(self):
        cases = {
            401: ("Unauthorized", "proveri token"),
            404: ("Not Found", "proveri token"),
            400: ("Bad Request: chat not found", "/start"),
            403: ("Forbidden: bot can't initiate conversation with a user", "/start"),
        }
        for status, (description, hint) in cases.items():
            with self.subTest(status=status):
                body = {"ok": False, "error_code": status, "description": description}
                with StubServer(lambda r: (status, body)) as server:
                    with self.assertRaises(NotifyError) as caught:
                        TelegramChannel(TOKEN, ["42"], server.url).send(MESSAGE)
                self.assertIn(hint, str(caught.exception))

    def test_other_errors_get_no_hint(self):
        with StubServer(lambda r: (429, {"ok": False, "description": "Too Many Requests: retry after 5"})) as server:
            with self.assertRaises(NotifyError) as caught:
                TelegramChannel(TOKEN, ["42"], server.url).send(MESSAGE)
        self.assertNotIn("proveri", str(caught.exception))

    def test_connection_errors_become_notify_errors(self):
        channel = TelegramChannel(TOKEN, ["42"], f"http://127.0.0.1:{closed_port()}", timeout=2)
        with self.assertRaises(NotifyError) as caught:
            channel.send(MESSAGE)
        self.assertNotIn("SECRET-TOKEN", str(caught.exception))

    def test_a_token_that_breaks_the_url_is_not_leaked(self):
        with self.assertRaises(NotifyError) as caught:
            TelegramChannel("bad token\n", ["42"], "http://127.0.0.1:9").send(MESSAGE)
        self.assertNotIn("bad token", str(caught.exception))

    def test_a_malformed_api_base_does_not_leak_the_token_either(self):
        # urllib quotes the whole URL, token included, in "unknown url type"
        with self.assertRaises(NotifyError) as caught:
            TelegramChannel(TOKEN, ["42"], "api.telegram.org").send(MESSAGE)
        self.assertNotIn("SECRET-TOKEN", str(caught.exception))

    def test_an_empty_token_does_not_garble_error_messages(self):
        with self.assertRaises(NotifyError) as caught:
            TelegramChannel("", ["42"], f"http://127.0.0.1:{closed_port()}", timeout=2).send(MESSAGE)
        self.assertNotIn("***", str(caught.exception))

    def test_a_non_json_answer_is_an_error(self):
        with StubServer(lambda r: (200, "not an object")) as server:
            with self.assertRaises(NotifyError):
                TelegramChannel(TOKEN, ["42"], server.url).send(MESSAGE)

    def test_one_reachable_chat_is_enough(self):
        def respond(request):
            if request.json()["chat_id"] == "bad":
                return 400, {"ok": False, "description": "Bad Request: chat not found"}
            return 200, {"ok": True, "result": {}}

        with StubServer(respond) as server:
            with self.assertLogs("powerout.notify", "WARNING") as logs:
                TelegramChannel(TOKEN, ["bad", "good"], server.url).send(MESSAGE)
        self.assertIn("chat not found", logs.output[0])

    def test_failing_for_every_chat_is_an_error(self):
        with StubServer(lambda r: (400, {"ok": False, "description": "chat not found"})) as server:
            with self.assertRaises(NotifyError) as caught:
                TelegramChannel(TOKEN, ["1", "2"], server.url).send(MESSAGE)
        self.assertIn("chat 1", str(caught.exception))
        self.assertIn("chat 2", str(caught.exception))

    def test_no_chat_id_is_an_error(self):
        with self.assertRaises(NotifyError):
            TelegramChannel(TOKEN, []).send(MESSAGE)

    def test_recent_chats_lists_who_wrote_to_the_bot(self):
        updates = [
            {"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "first_name": "Sava", "username": "sava"}}},
            {"update_id": 2, "message": {"chat": {"id": 42, "type": "private", "first_name": "Sava", "username": "sava"}}},
            {"update_id": 3, "channel_post": {"chat": {"id": -100, "type": "channel", "title": "Struja"}}},
            {"update_id": 4},
        ]
        with StubServer(lambda r: (200, {"ok": True, "result": updates})) as server:
            chats = TelegramChannel(TOKEN, [], server.url).recent_chats()
        self.assertEqual(chats, {"42": "Sava (@sava)", "-100": "Struja"})
        self.assertEqual(server.requests[0].path, f"/bot{TOKEN}/getUpdates")


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL and remembers what was done to it."""

    instances: list["FakeSMTP"] = []
    fail_with: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.calls = []
        self.mail = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, mail):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        self.calls.append("send")
        self.mail = mail


class EmailTest(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances = []
        FakeSMTP.fail_with = None
        patches = [mock.patch.object(smtplib, "SMTP", FakeSMTP), mock.patch.object(smtplib, "SMTP_SSL", FakeSMTP)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def channel(self, **overrides):
        values = dict(
            host="smtp.example.com",
            username="me@example.com",
            password="hunter2",
            to=["me@example.com", "partner@example.com"],
        )
        return EmailChannel(Email(**{**values, **overrides}))

    def test_builds_the_mail_and_logs_in(self):
        self.channel().send(MESSAGE)
        [smtp] = FakeSMTP.instances
        self.assertEqual((smtp.host, smtp.port), ("smtp.example.com", 465))
        self.assertEqual(smtp.calls, [("login", "me@example.com", "hunter2"), "send"])
        self.assertEqual(smtp.mail["Subject"], MESSAGE.title)
        self.assertEqual(smtp.mail["From"], "me@example.com")
        self.assertEqual(smtp.mail["To"], "me@example.com, partner@example.com")
        self.assertEqual(smtp.mail["Importance"], "high")
        self.assertEqual(smtp.mail.get_content().strip(), MESSAGE.body)
        self.assertTrue(smtp.mail["Message-ID"].endswith("@example.com>"))

    def test_starttls_upgrades_before_logging_in(self):
        self.channel(security="starttls").send(MESSAGE)
        [smtp] = FakeSMTP.instances
        self.assertEqual(smtp.port, 587)
        self.assertEqual(smtp.calls, ["starttls", ("login", "me@example.com", "hunter2"), "send"])

    def test_no_login_without_a_username(self):
        self.channel(username="", sender="alerts@example.com", security="none").send(MESSAGE)
        self.assertEqual(FakeSMTP.instances[0].calls, ["send"])

    def test_a_sender_with_a_display_name_still_gets_a_message_id(self):
        self.channel(sender="Struja <alerts@example.com>").send(MESSAGE)
        self.assertTrue(FakeSMTP.instances[0].mail["Message-ID"].endswith("@example.com>"))

    def test_smtp_errors_become_notify_errors_without_the_password(self):
        FakeSMTP.fail_with = smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
        with self.assertRaises(NotifyError) as caught:
            self.channel().send(MESSAGE)
        self.assertIn("SMTPAuthenticationError", str(caught.exception))
        self.assertNotIn("hunter2", str(caught.exception))

    def test_network_errors_become_notify_errors(self):
        FakeSMTP.fail_with = ConnectionRefusedError("refused")
        with self.assertRaises(NotifyError):
            self.channel().send(MESSAGE)

    def test_a_header_the_library_refuses_is_a_failure_not_a_crash(self):
        with self.assertRaises(NotifyError) as caught:
            self.channel().send(Message("naslov\nBcc: x@y.z", "telo"))
        self.assertIn("ValueError", str(caught.exception))
        self.assertEqual(FakeSMTP.instances, [])  # it never got as far as connecting


class FakeChannel:
    def __init__(self, name, error=None):
        self.name = name
        self.error = error
        self.sent = []

    def send(self, message):
        if self.error:
            raise NotifyError(self.error)
        self.sent.append(message)


class NotifierTest(unittest.TestCase):
    def test_all_mode_uses_every_channel(self):
        a, b = FakeChannel("email"), FakeChannel("telegram")
        attempts = Notifier([a, b], "all").deliver(MESSAGE)
        self.assertEqual([x.channel for x in attempts], ["email", "telegram"])
        self.assertEqual((len(a.sent), len(b.sent)), (1, 1))

    def test_fallback_mode_stops_at_the_first_success(self):
        a, b = FakeChannel("email"), FakeChannel("telegram")
        Notifier([a, b], "fallback").deliver(MESSAGE)
        self.assertEqual((len(a.sent), len(b.sent)), (1, 0))

    def test_fallback_mode_moves_on_after_a_failure(self):
        a, b = FakeChannel("email", error="smtp down"), FakeChannel("telegram")
        attempts = Notifier([a, b], "fallback").deliver(MESSAGE)
        self.assertEqual([x.error for x in attempts], ["smtp down", None])
        self.assertEqual(len(b.sent), 1)

    def test_one_failing_channel_does_not_block_the_other(self):
        a, b = FakeChannel("email", error="smtp down"), FakeChannel("telegram")
        with self.assertLogs("powerout.notify", "WARNING"):
            self.assertTrue(Notifier([a, b], "all").send(MESSAGE))
        self.assertEqual(len(b.sent), 1)

    def test_send_is_false_when_everything_fails(self):
        channels = [FakeChannel("email", error="x"), FakeChannel("telegram", error="y")]
        with self.assertLogs("powerout.notify", "WARNING"):
            self.assertFalse(Notifier(channels).send(MESSAGE))

    def test_delivery_can_be_overridden_per_call(self):
        a, b = FakeChannel("email"), FakeChannel("telegram")
        Notifier([a, b], "fallback").deliver(MESSAGE, delivery="all")
        self.assertEqual((len(a.sent), len(b.sent)), (1, 1))


class BuildNotifierTest(unittest.TestCase):
    def test_email_comes_before_telegram(self):
        cfg = Config.from_dict(
            {
                "telegram": {"token": "t", "chat_id": "1"},
                "email": {"host": "h", "sender": "a@b", "to": ["a@b"]},
                "general": {"delivery": "fallback"},
            }
        )
        notifier = build_notifier(cfg)
        self.assertEqual([c.name for c in notifier.channels], ["email", "telegram"])
        self.assertEqual(notifier.delivery, "fallback")

    def test_needs_at_least_one_channel(self):
        with self.assertRaises(ConfigError) as caught:
            build_notifier(Config.from_dict({}))
        self.assertIn("[telegram]", str(caught.exception))

    def test_telegram_needs_a_chat_id(self):
        with self.assertRaises(ConfigError) as caught:
            build_notifier(Config.from_dict({"telegram": {"token": "t"}}))
        self.assertIn("telegram-chat-id", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
