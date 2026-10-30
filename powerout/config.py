"""Configuration: a TOML file turned into validated dataclasses.

Error messages are meant for the user, so they are in Serbian like the rest of the interface.
"""

from __future__ import annotations

import logging
import os
import socket
import tomllib
from dataclasses import dataclass, field, fields
from datetime import tzinfo
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

TYPE_NAMES = {"str": "tekst", "int": "ceo broj", "float": "broj", "list[str]": "lista"}
MAX_NAME = 64  # the watchdog refuses longer location names


class ConfigError(Exception):
    """The configuration is missing or invalid; the message is meant for the user."""


def _one_of(obj, option: str, allowed: tuple[str, ...]) -> None:
    if getattr(obj, option) not in allowed:
        choices = ", ".join(f'"{a}"' for a in allowed)
        raise ConfigError(f"[{obj.section}] {option}: dozvoljene vrednosti su {choices}")


def _at_least(obj, option: str, minimum: float) -> None:
    if getattr(obj, option) < minimum:
        raise ConfigError(f"[{obj.section}] {option}: mora biti najmanje {minimum:g}")


def _required(obj, option: str) -> None:
    if not getattr(obj, option):
        raise ConfigError(f"[{obj.section}] {option}: obavezno polje")


def _single_word(obj, option: str) -> None:
    """Strip stray whitespace from a pasted token/URL; whitespace or control characters inside mean a bad paste."""
    value = getattr(obj, option).strip()
    if not value.isprintable() or any(c.isspace() for c in value):
        raise ConfigError(f"[{obj.section}] {option}: sadrži razmak ili nevidljiv znak (loše kopiranje?)")
    setattr(obj, option, value)


def _http_url(obj, option: str) -> None:
    _single_word(obj, option)
    if getattr(obj, option) and not getattr(obj, option).startswith(("http://", "https://")):
        raise ConfigError(f"[{obj.section}] {option}: mora početi sa http:// ili https://")


@dataclass
class General:
    section: ClassVar[str] = "general"

    name: str = ""  # shown in every message; also how the watchdog tells locations apart
    timezone: str = ""  # e.g. "Europe/Belgrade"; empty = system time zone
    delivery: str = "all"  # "all" = every channel, "fallback" = stop at the first one that works
    state_dir: str = ""  # where state files live; empty = a sensible default

    def __post_init__(self) -> None:
        self.name = self.name.strip() or socket.gethostname()
        if not self.name.isprintable() or len(self.name) > MAX_NAME:  # it ends up in subjects and headers
            raise ConfigError(f"[general] name: najviše {MAX_NAME} znakova, bez novog reda i nevidljivih znakova")
        _one_of(self, "delivery", ("all", "fallback"))
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ConfigError(
                    f'[general] timezone: nepoznata vremenska zona "{self.timezone}" (npr. "Europe/Belgrade")'
                ) from None

    @property
    def tz(self) -> tzinfo | None:
        return ZoneInfo(self.timezone) if self.timezone else None

    @property
    def state_path(self) -> Path:
        if self.state_dir:
            return Path(self.state_dir).expanduser()
        systemd = os.environ.get("STATE_DIRECTORY")  # set by systemd for units with StateDirectory=
        if systemd:
            return Path(systemd.split(":")[0])
        base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
        return Path(base) / "powerout"


@dataclass
class Telegram:
    section: ClassVar[str] = "telegram"

    token: str = field(default="", repr=False)
    chat_id: list[str] = field(default_factory=list)
    api_base: str = "https://api.telegram.org"  # only differs for a self-hosted Bot API server

    def __post_init__(self) -> None:
        _single_word(self, "token")
        _required(self, "token")
        _http_url(self, "api_base")
        # chat_id is checked when the channel is built: `telegram-chat-id` needs a config without it


@dataclass
class Email:
    section: ClassVar[str] = "email"

    host: str = ""
    port: int = 0  # 0 = the default for the chosen security
    security: str = "ssl"  # "ssl" (465), "starttls" (587) or "none" (25)
    username: str = ""
    password: str = field(default="", repr=False)
    sender: str = ""  # empty = username
    to: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _one_of(self, "security", ("ssl", "starttls", "none"))
        _required(self, "host")
        _required(self, "to")
        self.port = self.port or {"ssl": 465, "starttls": 587, "none": 25}[self.security]
        self.sender = self.sender or self.username
        if not self.sender:
            raise ConfigError("[email] sender: obavezno polje (ili podesi username)")


@dataclass
class Power:
    section: ClassVar[str] = "power"

    source: str = "auto"  # auto | sysfs | nut | command | none
    supply: str = ""  # sysfs: read only /sys/class/power_supply/<supply>
    ups: str = ""  # nut: UPS name, e.g. "myups@localhost"
    command: str = ""  # command: exit 0 = mains, 1 = on battery, anything else = unknown
    debounce_seconds: float = 10.0  # shorter flickers are ignored
    poll_seconds: float = 2.0

    def __post_init__(self) -> None:
        _one_of(self, "source", ("auto", "sysfs", "nut", "command", "none"))
        if self.source == "nut":
            _required(self, "ups")
        if self.source == "command":
            _required(self, "command")
        _at_least(self, "debounce_seconds", 0)
        _at_least(self, "poll_seconds", 0.1)


@dataclass
class Heartbeat:
    section: ClassVar[str] = "heartbeat"

    url: str = ""  # watchdog URL, or any ping URL (e.g. healthchecks.io); empty = no heartbeats
    token: str = field(default="", repr=False)
    interval_seconds: float = 60.0

    def __post_init__(self) -> None:
        _http_url(self, "url")
        _single_word(self, "token")
        _at_least(self, "interval_seconds", 1)


@dataclass
class Watchdog:
    section: ClassVar[str] = "watchdog"

    host: str = "127.0.0.1"  # use 0.0.0.0 only when nothing (e.g. a reverse proxy) sits in front
    port: int = 8787
    token: str = field(default="", repr=False)
    timeout_seconds: float = 180.0  # silence longer than this raises the alarm

    def __post_init__(self) -> None:
        if not 0 < self.port < 65536:
            raise ConfigError("[watchdog] port: mora biti između 1 i 65535")
        _single_word(self, "token")
        _at_least(self, "timeout_seconds", 3)


_SECTIONS = (General, Telegram, Email, Power, Heartbeat, Watchdog)
_OPTIONAL = (Telegram, Email)


def _coerce(section: str, option: str, value, hint: str):
    if hint == "str":
        ok = isinstance(value, str)
    elif hint == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif hint == "float":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        value = float(value) if ok else value
    else:  # list[str]; a single value is accepted as a one-item list
        items = value if isinstance(value, list) else [value]
        ok = all(isinstance(i, (str, int)) and not isinstance(i, bool) for i in items)
        value = [str(i) for i in items] if ok else value
    if not ok:
        raise ConfigError(f"[{section}] {option}: očekivana vrednost je {TYPE_NAMES[hint]}")
    return value


def _build(klass, table):
    if table is None:
        return None if klass in _OPTIONAL else klass()
    if not isinstance(table, dict):
        raise ConfigError(f"[{klass.section}] mora biti sekcija (tabela)")
    known = {f.name: f for f in fields(klass)}
    unknown = sorted(set(table) - set(known))
    if unknown:
        raise ConfigError(
            f"[{klass.section}] nepoznata opcija: {', '.join(unknown)} (dozvoljene su: {', '.join(known)})"
        )
    return klass(**{k: _coerce(klass.section, k, v, known[k].type) for k, v in table.items()})


@dataclass
class Config:
    general: General
    power: Power
    heartbeat: Heartbeat
    watchdog: Watchdog
    telegram: Telegram | None = None
    email: Email | None = None
    path: Path | None = None

    @classmethod
    def from_dict(cls, raw: dict, path: Path | None = None) -> Config:
        names = [k.section for k in _SECTIONS]
        unknown = sorted(set(raw) - set(names))
        if unknown:
            raise ConfigError(f"nepoznata sekcija [{unknown[0]}] (dozvoljene su: {', '.join(names)})")
        built = {k.section: _build(k, raw.get(k.section)) for k in _SECTIONS}
        return cls(path=path, **built)


def default_paths() -> list[Path]:
    paths = [Path(p) for p in [os.environ.get("POWEROUT_CONFIG")] if p]
    credentials = os.environ.get("CREDENTIALS_DIRECTORY")  # set by systemd for units with LoadCredential=
    if credentials:
        paths.append(Path(credentials) / "config.toml")
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return paths + [Path("config.toml"), xdg / "powerout" / "config.toml", Path("/etc/powerout/config.toml")]


def find(explicit: str | None = None) -> Path | None:
    """The config file to use: the explicit path if given, else the first default location that exists."""
    if explicit:
        return Path(explicit)
    return next((p for p in default_paths() if p.is_file()), None)


def load(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
        mode = path.stat().st_mode
    except OSError as e:
        raise ConfigError(f"ne mogu da pročitam {path}: {e.strerror}") from None
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: neispravan TOML: {e}") from None
    if mode & 0o077:
        log.warning("%s holds secrets but is readable by other users; run: chmod 600 %s", path, path)
    return Config.from_dict(raw, path)
