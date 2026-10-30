"""Notification channels (Telegram, e-mail) and the Notifier that fans a message out to them."""

from __future__ import annotations

import email.utils
import http.client
import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from .config import Config, ConfigError, Email

log = logging.getLogger(__name__)


class NotifyError(Exception):
    """One channel failed to deliver. The text is safe to log: it never contains secrets."""


@dataclass(frozen=True)
class Message:
    title: str
    body: str = ""

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.body}" if self.body else self.title


class Channel(Protocol):
    name: str

    def send(self, message: Message) -> None:
        """Deliver the message or raise NotifyError."""


class TelegramChannel:
    name = "telegram"

    def __init__(
        self, token: str, chat_ids: list[str], api_base: str = "https://api.telegram.org", timeout: float = 15.0
    ):
        self._token = token
        self._chat_ids = chat_ids
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def send(self, message: Message) -> None:
        if not self._chat_ids:
            raise NotifyError("telegram: nije podešen chat_id")
        failures = []
        for chat_id in self._chat_ids:
            try:
                self.call("sendMessage", {"chat_id": chat_id, "text": message.text})
            except NotifyError as e:
                failures.append(f"chat {chat_id}: {e}")
        if len(failures) == len(self._chat_ids):
            raise NotifyError("; ".join(failures))
        for failure in failures:  # delivered to somebody, so do not retry (that would spam the others)
            log.warning("telegram: %s", failure)

    def recent_chats(self) -> dict[str, str]:
        """chat_id -> description of the chats that recently wrote to the bot."""
        chats: dict[str, str] = {}
        for update in self.call("getUpdates"):
            for key in ("message", "edited_message", "channel_post", "my_chat_member"):
                chat = (update.get(key) or {}).get("chat")
                if chat:
                    names = [chat.get("title"), chat.get("first_name"), chat.get("last_name")]
                    label = " ".join(n for n in names if n)
                    if chat.get("username"):
                        label += f" (@{chat['username']})"
                    chats[str(chat["id"])] = label or chat.get("type", "")
        return chats

    def call(self, method: str, payload: dict | None = None):
        try:
            request = urllib.request.Request(
                f"{self._api_base}/bot{self._token}/{method}",
                data=None if payload is None else json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as e:
            with e:  # closes the connection
                description = _api_error(e)
            raise NotifyError(self._scrub(f"telegram: {description}")) from None
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise NotifyError(self._scrub(f"telegram: {e}")) from None
        if not isinstance(data, dict) or not data.get("ok"):
            raise NotifyError("telegram: neočekivan odgovor servera")
        return data["result"]

    def _scrub(self, text: str) -> str:
        # The token is part of the request URL, which some exception messages quote (even repr-escaped).
        for form in filter(None, (self._token, repr(self._token)[1:-1])):
            text = text.replace(form, "***")
        return text


def _api_error(error: urllib.error.HTTPError) -> str:
    try:
        body = json.load(error)
    except (OSError, ValueError):
        body = None
    description = body.get("description") if isinstance(body, dict) else None
    text = f"HTTP {error.code}: {description}" if description else f"HTTP {error.code}"
    # The two usual mistakes when setting up a bot: a wrong token (Telegram answers 401, or 404 for a
    # malformed one) and a chat that never pressed /start (400 "chat not found" or 403).
    if error.code in (401, 404):
        return f"{text} (proveri token)"
    if error.code in (400, 403):
        return f"{text} (proveri chat_id i da li si botu poslao /start)"
    return text


class EmailChannel:
    name = "email"

    def __init__(self, cfg: Email, timeout: float = 20.0):
        self._cfg = cfg
        self._timeout = timeout

    def send(self, message: Message) -> None:
        cfg = self._cfg
        context = ssl.create_default_context()
        try:
            mail = self._compose(message)
            if cfg.security == "ssl":
                client = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=self._timeout, context=context)
            else:
                client = smtplib.SMTP(cfg.host, cfg.port, timeout=self._timeout)
            with client:
                if cfg.security == "starttls":
                    client.starttls(context=context)
                if cfg.username:
                    client.login(cfg.username, cfg.password)
                client.send_message(mail)
        # ValueError: a header the e-mail library refuses (e.g. a line break). That can never succeed,
        # so it must surface as a normal failure instead of escaping the channel.
        except (smtplib.SMTPException, OSError, ValueError) as e:
            raise NotifyError(f"email: {type(e).__name__}: {e}") from None

    def _compose(self, message: Message) -> EmailMessage:
        cfg = self._cfg
        mail = EmailMessage()
        mail["Subject"] = message.title
        mail["From"] = cfg.sender
        mail["To"] = ", ".join(cfg.to)
        mail["Date"] = email.utils.formatdate(localtime=True)
        mail["Message-ID"] = email.utils.make_msgid(domain=email.utils.parseaddr(cfg.sender)[1].partition("@")[2] or None)
        mail["X-Priority"] = "1"
        mail["Importance"] = "high"
        mail.set_content(message.body or message.title)
        return mail


@dataclass(frozen=True)
class Attempt:
    channel: str
    error: str | None


class Notifier:
    """Sends a message through the configured channels."""

    def __init__(self, channels: list[Channel], delivery: str = "all"):
        self.channels = channels
        self.delivery = delivery

    def deliver(self, message: Message, delivery: str | None = None) -> list[Attempt]:
        """Try the channels and report what happened on each one. "fallback" stops at the first success."""
        mode = delivery or self.delivery
        attempts = []
        for channel in self.channels:
            try:
                channel.send(message)
            except NotifyError as e:
                attempts.append(Attempt(channel.name, str(e)))
                continue
            attempts.append(Attempt(channel.name, None))
            log.info("sent %r via %s", message.title, channel.name)
            if mode == "fallback":
                break
        return attempts

    def send(self, message: Message) -> bool:
        """True if the message reached at least one channel."""
        attempts = self.deliver(message)
        for attempt in attempts:
            if attempt.error:
                log.warning("delivery failed: %s", attempt.error)
        return any(a.error is None for a in attempts)


def build_notifier(cfg: Config) -> Notifier:
    channels: list[Channel] = []
    if cfg.email:
        channels.append(EmailChannel(cfg.email))
    if cfg.telegram:
        if not cfg.telegram.chat_id:
            raise ConfigError("[telegram] chat_id: obavezno polje (pokreni `powerout telegram-chat-id` da ga pronađeš)")
        channels.append(TelegramChannel(cfg.telegram.token, cfg.telegram.chat_id, cfg.telegram.api_base))
    if not channels:
        raise ConfigError("nije podešen nijedan kanal za obaveštenja: dodaj sekciju [telegram] i/ili [email]")
    return Notifier(channels, cfg.general.delivery)
