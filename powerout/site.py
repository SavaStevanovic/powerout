"""The personal website: saved addresses, a poller that reads the distributor's pages, and the web handlers."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, fields
from datetime import datetime, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import pages
from .config import Config, ConfigError
from .messages import Formatter
from .notify import Notifier, build_notifier
from .outages import AREAS, Address, AreaData, Fetcher, find_matches
from .serbian import fold
from .store import JsonStore

log = logging.getLogger(__name__)

MAX_ADDRESSES = 10
MAX_FORM = 8192
MIN_REFRESH_GAP = 60.0  # seconds between two reads of the distributor's pages that a person asks for
REMIND_FROM_HOUR = 6  # the same-day reminder is not sent in the small hours, when the pages are republished
KEEP_SENT = 10 * 86400  # how long the record of a sent notice is kept
CHANNEL_NAMES = {"telegram": "Telegram", "email": "e-mail"}


# --- saved addresses ----------------------------------------------------------------------------


class AddressError(ValueError):
    """The form was not acceptable; the message says what to change."""


def _text(form: dict[str, str], name: str, limit: int, label: str, *, required: bool = False) -> str:
    value = " ".join(form.get(name, "").split())
    if required and not value:
        raise AddressError(f"{label}: ovo polje je obavezno.")
    if len(value) > limit or not value.isprintable():
        raise AddressError(f"{label}: najviše {limit} znakova, bez posebnih znakova.")
    return value


def _same(a: Address, b: Address) -> bool:
    return a.area == b.area and all(fold(getattr(a, f)) == fold(getattr(b, f)) for f in ("place", "street", "number"))


class AddressBook:
    """The addresses a person asked to watch, kept in a JSON file."""

    def __init__(self, store: JsonStore):
        self._store = store
        self._lock = threading.Lock()
        self._items: list[Address] = []
        known = {f.name for f in fields(Address)}
        for raw in (store.load() or {}).get("addresses", []):
            try:
                address = Address(**{k: v for k, v in raw.items() if k in known})
            except (TypeError, AttributeError):
                continue
            if address.area in AREAS and address.id and address.place:
                self._items.append(address)

    def all(self) -> list[Address]:
        with self._lock:
            return list(self._items)

    def add(self, form: dict[str, str]) -> Address:
        if form.get("area") not in AREAS:
            raise AddressError("Izaberi oblast distributera.")
        address = Address(
            id=secrets.token_hex(4),
            area=form["area"],
            place=_text(form, "place", 60, "Mesto", required=True),
            street=_text(form, "street", 80, "Ulica"),
            number=_text(form, "number", 12, "Kućni broj"),
            label=_text(form, "label", 40, "Naziv"),
            notify=form.get("notify") == "on",
        )
        with self._lock:
            if len(self._items) >= MAX_ADDRESSES:
                raise AddressError(f"Može najviše {MAX_ADDRESSES} adresa. Ukloni neku pa dodaj novu.")
            if any(_same(a, address) for a in self._items):
                raise AddressError("Ova adresa je već sačuvana.")
            self._items.append(address)
            self._save()
        return address

    def remove(self, address_id: str) -> bool:
        with self._lock:
            kept = [a for a in self._items if a.id != address_id]
            changed = len(kept) != len(self._items)
            self._items = kept
            if changed:
                self._save()
            return changed

    def toggle_notify(self, address_id: str) -> bool | None:
        """Switch notifications of an address; returns the new setting, or None if there is no such address."""
        with self._lock:
            for i, a in enumerate(self._items):
                if a.id == address_id:
                    self._items[i] = Address(**{**asdict(a), "notify": not a.notify})
                    self._save()
                    return not a.notify
        return None

    def _save(self) -> None:
        self._store.save({"addresses": [asdict(a) for a in self._items]})


class SentLog:
    """Which announcements were already sent to the person, so that each is sent once, and reminded of once."""

    def __init__(self, store: JsonStore):
        self._store = store
        self._lock = threading.Lock()
        loaded = (store.load() or {}).get("sent", {})
        self._sent: dict[str, dict] = {k: v for k, v in loaded.items() if isinstance(v, dict) and "announced" in v}

    def get(self, key: str) -> dict | None:
        with self._lock:
            return dict(self._sent[key]) if key in self._sent else None

    def mark(self, key: str, kind: str, now: float) -> None:
        """kind is "announced" or "reminded"."""
        with self._lock:
            entry = self._sent.setdefault(key, {"announced": now, "reminded": None})
            entry[kind] = now
            self._sent = {k: v for k, v in self._sent.items() if now - v["announced"] < KEEP_SENT}
            self._store.save({"sent": self._sent})


# --- reading the distributor's pages and telling the person --------------------------------------


class Poller:
    """Keeps the latest pages of every area that has an address, and sends the notices that are due."""

    def __init__(
        self,
        *,
        fetcher: Fetcher,
        book: AddressBook,
        sent: SentLog,
        notifier: Notifier | None,
        formatter: Formatter,
        tz: tzinfo | None,
        interval: float,
        clock: Callable[[], float] = time.time,
    ):
        self._fetcher = fetcher
        self._book = book
        self._sent = sent
        self._notifier = notifier
        self._formatter = formatter
        self._tz = tz
        self._interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, AreaData] = {}
        self._asked = 0.0
        self._wake = threading.Event()
        self._stopping = False
        self.busy = False

    def snapshot(self) -> dict[str, AreaData]:
        with self._lock:
            return dict(self._data)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), self._tz)

    def request_refresh(self, *, force: bool = False) -> bool:
        """Ask for a read right now. A person pressing the button is refused if a read was asked for a moment
        ago; a new address is not (its area has not been read yet)."""
        if not force and self._clock() - self._asked < MIN_REFRESH_GAP:
            return False
        self._asked = self._clock()
        self._wake.set()
        return True

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    def run(self) -> None:
        while not self._stopping:
            self._wake.clear()
            try:
                self.refresh()
            except Exception:
                log.exception("reading the distributor's pages failed")
            self._wake.wait(self._interval)

    def refresh(self) -> None:
        self.busy = True
        try:
            now = self.now()
            for area in sorted({a.area for a in self._book.all()}):
                data = self._fetcher.fetch(area, now.date())
                for error in data.errors:
                    log.warning("%s", error)
                with self._lock:
                    self._data[area] = data
            self._notify(self.now())
        finally:
            self.busy = False

    def _notify(self, now: datetime) -> None:
        if self._notifier is None:
            return
        snapshot = self.snapshot()
        for address in self._book.all():
            data = snapshot.get(address.area)
            if not address.notify or data is None:
                continue
            for match in find_matches(address, list(data.pages)):
                if match.level not in ("likely", "possible") or pages.finished(match, now):
                    continue
                seen = self._sent.get(match.key)
                reminder = False
                if seen is not None:
                    announced_on = datetime.fromtimestamp(seen["announced"], self._tz).date()
                    due = match.day == now.date() and seen.get("reminded") is None and announced_on < now.date()
                    if not due or now.hour < REMIND_FROM_HOUR:
                        continue
                    reminder = True
                message = self._formatter.planned_outage(address.title, address.line, match, now.date(), reminder=reminder)
                if self._notifier.send(message):  # if it failed, the next read tries again
                    self._sent.mark(match.key, "reminded" if reminder else "announced", self._clock())


# --- what the handlers work with ----------------------------------------------------------------


class Site:
    def __init__(self, *, book: AddressBook, poller: Poller, notifier: Notifier | None, formatter: Formatter, interval_minutes: int, name: str):
        self.book = book
        self.poller = poller
        self.notifier = notifier
        self._formatter = formatter
        self._interval = interval_minutes
        self._name = name
        self._flash = ""
        self._lock = threading.Lock()

    def say(self, text: str) -> None:
        with self._lock:
            self._flash = text

    def view(self, *, form: dict[str, str] | None = None, error: str = "") -> pages.View:
        with self._lock:
            flash, self._flash = self._flash, ""
        snapshot = self.poller.snapshot()
        results = []
        for address in self.book.all():
            data = snapshot.get(address.area)
            matches = tuple(find_matches(address, list(data.pages))) if data else ()
            results.append(pages.AddressResult(address, matches, data))
        places = {a.home for a in AREAS.values()}
        for data in snapshot.values():
            for page in data.pages:
                places.update(v for row in page.rows for v in (row.branch, row.municipality) if v)
        channels = " i ".join(CHANNEL_NAMES.get(c.name, c.name) for c in self.notifier.channels) if self.notifier else ""
        return pages.View(
            now=self.poller.now(),
            results=tuple(results),
            channels=channels,
            interval=self._interval,
            flash=flash,
            error=error,
            form=form or {},
            suggestions=tuple(sorted(places, key=fold)),
            busy=self.poller.busy,
        )

    def add(self, form: dict[str, str]) -> None:
        address = self.book.add(form)
        self.say(f"Sačuvano: {address.title}. Čitam najave distributera.")
        self.poller.request_refresh(force=True)

    def remove(self, address_id: str) -> None:
        self.say("Adresa je uklonjena." if self.book.remove(address_id) else "Te adrese nema.")

    def toggle(self, address_id: str) -> None:
        state = self.book.toggle_notify(address_id)
        self.say("Te adrese nema." if state is None else "Obaveštenja su uključena." if state else "Obaveštenja su isključena.")

    def refresh(self) -> None:
        self.say(
            "Čitam najave distributera; osvežiću stranicu."
            if self.poller.request_refresh()
            else "Najave su upravo pročitane; pokušaj za minut."
        )

    def test_notification(self) -> None:
        if self.notifier is None:
            self.say("Obaveštenja nisu podešena. Dodaj sekciju [telegram] ili [email] u config.toml.")
            return
        results = self.notifier.deliver(self._formatter.test(self._name), delivery="all")
        self.say(
            "Probna poruka: "
            + "; ".join(
                f"{CHANNEL_NAMES.get(r.channel, r.channel)} {'radi' if r.error is None else 'ne radi (' + r.error.replace(r.channel + ': ', '') + ')'}"
                for r in results
            )
            + "."
        )


# --- HTTP ---------------------------------------------------------------------------------------

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    # Not "no-referrer": under it a browser sends "Origin: null" with the page's own forms, and a request from
    # this page can no longer be told from one made by a stranger's page. "same-origin" keeps the origin for
    # the page's own requests and still tells other sites (the distributor's, linked to) nothing.
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}


class _Handler(BaseHTTPRequestHandler):
    server: SiteServer
    timeout = 10

    def version_string(self) -> str:
        return "powerout"

    def do_GET(self) -> None:
        if not self._host_ok():
            return self.send_error(400, "Nepoznat Host")
        path = urlsplit(self.path).path
        if path == "/health":
            return self._send(200, b"ok\n", "text/plain; charset=utf-8")
        if path != "/":
            return self.send_error(404)
        self._page(200, self.server.site.view())

    def do_POST(self) -> None:
        if not self._host_ok() or not self._same_origin():
            log.warning("refused a POST from %s: foreign origin", self.client_address[0])
            return self.send_error(403, "Zahtev nije sa ove stranice")
        form = self._form()
        if form is None:
            return self.send_error(400)
        site, path = self.server.site, urlsplit(self.path).path
        parts = path.strip("/").split("/")
        try:
            if path == "/address":
                site.add(form)
            elif len(parts) == 3 and parts[0] == "address" and parts[2] == "remove":
                site.remove(parts[1])
            elif len(parts) == 3 and parts[0] == "address" and parts[2] == "notify":
                site.toggle(parts[1])
            elif path == "/refresh":
                site.refresh()
            elif path == "/test-notification":
                site.test_notification()
            else:
                return self.send_error(404)
        except AddressError as e:
            return self._page(422, site.view(form=form, error=str(e)))
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _page(self, status: int, view: pages.View) -> None:
        self._send(status, pages.render(view).encode(), "text/html; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _form(self) -> dict[str, str] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if not 0 <= length <= MAX_FORM:
            return None
        parsed = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"), keep_blank_values=True)
        return {k: v[-1] for k, v in parsed.items()}

    def _host_ok(self) -> bool:
        """Refuse requests addressed to a name that is not ours: a web page on the internet can make a browser
        send requests to localhost under its own name (DNS rebinding)."""
        if self.server.any_host:
            return True
        host = urlsplit("//" + self.headers.get("Host", "")).hostname
        return host in self.server.hosts

    def _same_origin(self) -> bool:
        """A form on another website can post to localhost; only forms of this page may change anything.

        A browser says where a request comes from in Sec-Fetch-Site, which a page cannot forge. Older
        browsers, and anything on a plain-http address other than localhost, only send Origin or Referer.
        """
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site is not None:
            return fetch_site == "same-origin"  # not "same-site": another port on localhost is another program
        origin = self.headers.get("Origin") or self.headers.get("Referer")
        return bool(origin) and urlsplit(origin).netloc == self.headers.get("Host")

    def log_message(self, format: str, *args) -> None:
        log.debug("%s %s", self.client_address[0], format % args)


class SiteServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], site: Site):
        super().__init__(address, _Handler)
        self.site = site
        host = address[0]
        self.any_host = host in ("0.0.0.0", "::", "")
        self.hosts = {"localhost", "127.0.0.1", "::1", host}


def run_site(cfg: Config, stop: threading.Event) -> None:
    formatter = Formatter(cfg.general.tz)
    try:
        notifier = build_notifier(cfg)
    except ConfigError as e:
        log.info("notifications are off: %s", e)
        notifier = None
    state = cfg.general.state_path
    book = AddressBook(JsonStore(state / "addresses.json"))
    interval = cfg.site.poll_minutes * 60
    poller = Poller(
        fetcher=Fetcher(),
        book=book,
        sent=SentLog(JsonStore(state / "sent.json")),
        notifier=notifier,
        formatter=formatter,
        tz=cfg.general.tz,
        interval=interval,
    )
    site = Site(
        book=book,
        poller=poller,
        notifier=notifier,
        formatter=formatter,
        interval_minutes=round(cfg.site.poll_minutes),
        name=cfg.general.name,
    )
    try:
        server = SiteServer((cfg.site.host, cfg.site.port), site)
    except OSError as e:
        raise ConfigError(f"ne mogu da otvorim {cfg.site.host}:{cfg.site.port}: {e.strerror}") from None
    log.info(
        "site on http://%s:%d, %d address(es), notifications %s",
        cfg.site.host,
        cfg.site.port,
        len(book.all()),
        "on" if notifier else "off",
    )
    threading.Thread(target=poller.run, name="poller", daemon=True).start()
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    try:
        stop.wait()
    finally:
        poller.stop()
        server.shutdown()
        server.server_close()
