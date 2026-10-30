"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time

from . import __version__, config
from .agent import run_agent
from .config import Config, ConfigError
from .messages import Formatter
from .notify import NotifyError, TelegramChannel, build_notifier
from .power import SysfsSource, detect_source
from .site import run_site
from .watchdog import run_watchdog

POWER_TEXT = {True: "mrežno napajanje (struja postoji)", False: "baterija (struje nema)", None: "nepoznato"}


def _load(args: argparse.Namespace, *, required: bool = True) -> Config:
    path = config.find(args.config)
    if path is None:
        if required:
            raise ConfigError(
                "nisam našao config.toml; napravi ga ovako:\n"
                "  mkdir -p ~/.config/powerout && cp config.example.toml ~/.config/powerout/config.toml\n"
                "  chmod 600 ~/.config/powerout/config.toml"
            )
        return Config.from_dict({})
    return config.load(path)


def _stop_on_signal() -> threading.Event:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    return stop


def cmd_agent(args: argparse.Namespace) -> int:
    run_agent(_load(args), _stop_on_signal())
    return 0


def cmd_watchdog(args: argparse.Namespace) -> int:
    run_watchdog(_load(args), _stop_on_signal())
    return 0


def cmd_site(args: argparse.Namespace) -> int:
    run_site(_load(args, required=False), _stop_on_signal())
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    cfg = _load(args)
    notifier = build_notifier(cfg)
    attempts = notifier.deliver(Formatter(cfg.general.tz).test(cfg.general.name), delivery="all")
    for attempt in attempts:
        # the error text already starts with the channel's name
        detail = "OK" if attempt.error is None else "GREŠKA - " + attempt.error.removeprefix(attempt.channel + ": ")
        print(f"  {attempt.channel}: {detail}")
    return 0 if all(a.error is None for a in attempts) else 1


def cmd_check_power(args: argparse.Namespace) -> int:
    cfg = _load(args, required=False)
    source = detect_source(cfg.power)
    if source is None:
        print(
            "Ovaj uređaj nema šta da očitava (nema baterije ni UPS-a), pa ne može sam da primeti nestanak struje.\n"
            "Zato koristi [heartbeat] + watchdog na drugoj mašini (vidi README), ili podesi [power] za UPS/komandu."
        )
        return 1
    print(f"Izvor: {source.description}")
    if isinstance(source, SysfsSource):
        for supply in source.supplies():
            print(f"  {supply.name}: type={supply.kind} scope={supply.scope} online={supply.online}")
    last = object()
    while True:
        reading = source.read()
        if reading != last:
            print(f"{time.strftime('%H:%M:%S')}  {POWER_TEXT[reading]}")
            last = reading
        if not args.watch:
            return 0
        time.sleep(cfg.power.poll_seconds)


def cmd_telegram_chat_id(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if cfg.telegram is None:
        raise ConfigError("dodaj sekciju [telegram] sa token-om u config.toml")
    chats = TelegramChannel(cfg.telegram.token, [], cfg.telegram.api_base).recent_chats()
    if not chats:
        print("Bot nije dobio nijednu poruku. Otvori razgovor sa botom u Telegramu, pošalji mu /start i pokreni ponovo.")
        return 1
    for chat_id, label in chats.items():
        print(f'chat_id = "{chat_id}"   # {label}')
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="powerout", description="Javlja na Telegram i/ili e-mail kada nestane struja na tvojoj lokaciji."
    )
    parser.add_argument("-c", "--config", metavar="FAJL", help="putanja do config.toml (inače se traži na uobičajenim mestima)")
    parser.add_argument("-v", "--verbose", action="store_true", help="detaljniji log")
    parser.add_argument("--version", action="version", version=f"powerout {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="komanda")

    def add(name: str, handler, help: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help, description=help)
        sub.set_defaults(handler=handler)
        return sub

    add("agent", cmd_agent, "pokreni agenta na lokaciji koja se prati")
    add("watchdog", cmd_watchdog, "pokreni stražara: javlja kada agent prestane da se javlja")
    add("site", cmd_site, "pokreni sajt: proverava planirana isključenja struje za tvoju adresu")
    add("test", cmd_test, "pošalji probnu poruku na sve podešene kanale")
    add("telegram-chat-id", cmd_telegram_chat_id, "pronađi svoj Telegram chat_id (prvo pošalji botu /start)")
    check = add("check-power", cmd_check_power, "pokaži šta lokalni izvor trenutno očitava")
    check.add_argument("--watch", action="store_true", help="prati promene (izvuci i vrati punjač da probaš)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # journald stamps every line itself, so leave the time out when running under systemd
    fmt = "%(levelname)s %(name)s: %(message)s" if os.environ.get("JOURNAL_STREAM") else "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=fmt)
    try:
        return args.handler(args)
    except ConfigError as e:
        print(f"powerout: {e}", file=sys.stderr)
        return 2
    except NotifyError as e:
        print(f"powerout: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
