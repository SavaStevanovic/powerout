"""Planned outages published by the distributor: reading its pages, and checking an address against them.

The pages are HTML tables (municipality, time, streets) in Cyrillic. The streets cell is free text:
"КРАЉА ПЕТРА: 9-11," in Belgrade, "делови улица Требињске, Космајске" in Niš, a village name elsewhere.
So an address is matched by words, not by exact strings, and every match carries its reason and the
positions of the words that caused it, so a person can check the announcement for themselves.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import re
import time as clock
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, time, timedelta
from html.parser import HTMLParser

from .serbian import Token, find_phrase, fold, has_words, query_keys, to_latin, tokenize

log = logging.getLogger(__name__)

BASE_URL = "https://www.elektrodistribucija.rs"
DAYS = 4  # the distributor publishes today and the three days after
MAX_BYTES = 2_000_000
USER_AGENT = "powerout/0.1 (personal planned-outage checker)"
LEVELS = ("likely", "possible", "near", "area")  # most relevant first


@dataclass(frozen=True)
class Area:
    """One of the distributor's five regional pages."""

    key: str
    name: str
    home: str  # the city of a page that has no branch column (Belgrade)
    folder: str
    file: str  # {day} is 0 for today
    page: str  # where a person reads the same thing

    def url(self, day: int) -> str:
        return f"{BASE_URL}/{self.folder}/{self.file.format(day=day)}"

    @property
    def page_url(self) -> str:
        return f"{BASE_URL}/planirana-iskljucenja/{self.page}"


AREAS: dict[str, Area] = {
    a.key: a
    for a in (
        Area("bg", "Beograd", "Beograd", "planirana-iskljucenja-beograd", "Dan_{day}_Iskljucenja.htm", "planirana-bgd"),
        Area("ns", "Novi Sad", "Novi Sad", "planirana-iskljucenja-srbija", "NoviSad_Dan_{day}_Iskljucenja.htm", "planirana-ns"),
        Area("kv", "Kraljevo", "Kraljevo", "planirana-iskljucenja-srbija", "Kraljevo_Dan_{day}_Iskljucenja.htm", "planirana-kv"),
        Area("kg", "Kragujevac", "Kragujevac", "planirana-iskljucenja-srbija", "Kragujevac_Dan_{day}_Iskljucenja.htm", "planirana-kg"),
        Area("ni", "Niš", "Niš", "planirana-iskljucenja-srbija", "Nis_Dan_{day}_Iskljucenja.htm", "planirana-ni"),
    )
}


# --- reading a page -----------------------------------------------------------------------------


class ParseError(Exception):
    """The page is not the table we know."""


@dataclass(frozen=True)
class Row:
    branch: str  # "" on pages without that column
    municipality: str
    when: str  # the time cell as published, e.g. "09:00 - 10:00"
    start: time | None
    end: time | None
    text: str  # the streets cell, in Latin script


@dataclass(frozen=True)
class Page:
    day: date | None
    rows: tuple[Row, ...]


class _Tables(HTMLParser):
    """The text of every cell of every table row."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DMY_DATE = re.compile(r"(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})")
_TIMES = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})")


def _clean(cell: str) -> str:
    text = re.sub(r"\s+", " ", cell.replace("\xa0", " ")).strip()
    return to_latin(text).strip('"“”„ ').rstrip(";, ")


def _date(cell: str) -> date | None:
    try:
        if m := _ISO_DATE.search(cell):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := _DMY_DATE.search(cell):
            return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        pass
    return None


def _clock(hours: str, minutes: str) -> time | None:
    h, m = int(hours), int(minutes)
    if h == 24 and m == 0:
        return time(23, 59)  # "until midnight"
    return time(h, m) if h < 24 and m < 60 else None


def parse_page(raw: bytes) -> Page:
    """The announcements of one day. Raises ParseError if the page is not a table with the known headings."""
    parser = _Tables()
    parser.feed(raw.decode("utf-8-sig", errors="replace"))
    parser.close()
    rows = [[_clean(c) for c in row] for row in parser.rows]
    heading = next((i for i, row in enumerate(rows) if {"opstina", "vreme", "ulice"} <= {fold(c) for c in row}), None)
    if heading is None:
        raise ParseError("stranica nema tabelu sa kolonama Opština, Vreme i Ulice")
    names = [fold(c) for c in rows[heading]]
    column = {name: names.index(name) if name in names else None for name in ("ogranak", "opstina", "vreme", "ulice")}
    day = next((d for row in rows[:heading] for c in row if (d := _date(c))), None)
    found = []
    for row in rows[heading + 1 :]:
        if len(row) < len(names):
            continue
        when = row[column["vreme"]]
        span = _TIMES.search(when)
        found.append(
            Row(
                branch=row[column["ogranak"]] if column["ogranak"] is not None else "",
                municipality=row[column["opstina"]],
                when=when,
                start=_clock(span[1], span[2]) if span else None,
                end=_clock(span[3], span[4]) if span else None,
                text=row[column["ulice"]],
            )
        )
    return Page(day, tuple(found))


# --- house numbers ------------------------------------------------------------------------------

# "9-11", "4А", "19Г-19И", "109 – 153", "91 б", "ББ" (no number). A single letter may follow a number.
_LETTER = r"(?:[^\W\d_](?![^\W\d_]))?"
_NUMBER = rf"\d+\s?{_LETTER}"
_ITEM = rf"(?:{_NUMBER}(?:\s*[-–—]\s*{_NUMBER})?|[Bb][Bb](?![^\W_]))"
_LIST = re.compile(rf"\s*[:(]?\s*(?P<items>{_ITEM}(?:\s*,\s*{_ITEM})*)")
_PART = re.compile(r"(?P<lo>\d+)\s?(?P<lo_s>[^\W\d_]?)(?:\s*[-–—]\s*(?P<hi>\d+)\s?(?P<hi_s>[^\W\d_]?))?")


@dataclass(frozen=True)
class Numbers:
    lo: int
    hi: int | None  # None: a single house
    suffix: str  # the letter after a single house number ("91 б")


@dataclass(frozen=True)
class Listed:
    """House numbers found right after a street name."""

    items: list[Numbers]
    where: tuple[int, int] | None  # their position in the text that was searched
    # Only a list can rule a house out. A lone number in prose ("Вука Караџића 170 према аутопуту") often
    # means "from there on", so it must not be taken as the only affected house.
    reliable: bool


def parse_numbers(text: str) -> Listed:
    """The house numbers written at the very start of `text`."""
    m = _LIST.match(text)
    if not m:
        return Listed([], None, False)
    items = []
    for item in re.finditer(_ITEM, m["items"]):
        part = _PART.fullmatch(item.group())
        if part:  # "ББ" is not a number
            hi = int(part["hi"]) if part["hi"] else None
            items.append(Numbers(int(part["lo"]), hi, fold(part["lo_s"]) if hi is None else ""))
    lead = text[: m.start("items")]
    reliable = ":" in lead or "(" in lead or len(items) > 1 or any(n.hi is not None for n in items)
    return Listed(items, (m.start("items"), m.end("items")), reliable)


def parse_house(text: str) -> tuple[int, str] | None:
    """(number, letter) from what a person typed: "12", "12a", "91 б"."""
    m = re.match(r"\s*(\d+)\s?([^\W\d_]?)", to_latin(text))
    return (int(m[1]), fold(m[2])) if m else None


def covers(numbers: list[Numbers], house: tuple[int, str]) -> bool:
    """Whether the house is among the listed numbers.

    A range whose two ends have the same parity is one side of the street ("2-12" is the even
    numbers); one that mixes parities is every number between.
    """
    number, letter = house
    for n in numbers:
        if n.hi is None:
            if n.lo == number and (not n.suffix or not letter or n.suffix == letter):
                return True
        elif n.lo <= number <= n.hi and not ((n.hi - n.lo) % 2 == 0 and (number - n.lo) % 2 != 0):
            return True
    return False


# --- matching an address ------------------------------------------------------------------------


@dataclass(frozen=True)
class Address:
    id: str
    area: str
    place: str  # city, municipality or village, as a person says it
    street: str = ""
    number: str = ""
    label: str = ""
    notify: bool = True

    @property
    def line(self) -> str:
        """"Trebinjska 12, Niš"."""
        street = " ".join(part for part in (self.street, self.number) if part)
        return ", ".join(part for part in (street, self.place) if part)

    @property
    def title(self) -> str:
        return self.label or self.line


@dataclass(frozen=True)
class Match:
    # likely: the street (or village) is named; possible: the place is, but not the street;
    # near: the street is named but the house is outside the listed numbers; area: only the municipality or branch
    level: str
    reason: str
    day: date
    row: Row
    highlights: tuple[tuple[int, int], ...]  # character positions in row.text of the words that matched
    key: str  # the same announcement of the same address always has the same key
    area: str  # key of the AREAS entry the announcement came from


_OUTCOMES = ("street_number", "street_open", "street_ask_number", "number_outside")  # what a person most needs to know first


_SETTLEMENT = re.compile(r"\bnaselje\s+([^:]{1,60}):", re.IGNORECASE)


def _scope(text: str, tokens: list[Token], place: list[str]) -> list[Token]:
    """The words a street is looked for in.

    Belgrade lists whole municipalities in one row ("Naselje RIPANJ: ... Naselje SIBNICA: ..."), and the
    same street name recurs in several settlements. If the person named one of them, only its section counts.
    The caller passes the place only when it is narrower than the row's municipality: the streets before the
    first marker belong to the municipality itself, and a settlement may carry the municipality's name.
    """
    marks = list(_SETTLEMENT.finditer(text))
    for i, mark in enumerate(marks):
        if has_words(place, tokenize(mark[1])):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            return [t for t in tokens if mark.start() <= t.start < end]
    return tokens


def _structured(text: str) -> bool:
    """Whether the text is a list of streets with house numbers ("КРАЉА ПЕТРА: 9-11,")."""
    return bool(re.search(r"[^\W\d_]\s*:\s*\d", text))


def _spans(tokens: list[Token], indexes: list[int]) -> list[tuple[int, int]]:
    return [(tokens[i].start, tokens[i].end) for i in indexes]


def match_row(address: Address, row: Row, day: date, home: str = "") -> Match | None:
    text = row.text
    tokens = tokenize(text)
    place = query_keys(address.place)
    street = query_keys(address.street) if address.street.strip() else []
    in_fields = has_words(place, tokenize(f"{row.branch} {row.municipality}"))
    # A page without a branch column belongs to one city, so naming that city names the row's city.
    in_city = in_fields or (not row.branch and bool(home) and has_words(place, tokenize(home)))
    place_hits = find_phrase(place, tokens)
    place_ok = not place or in_city or bool(place_hits)

    highlights: list[tuple[int, int]] = []
    level = reason = None
    if street:
        scoped = tokens if in_city else _scope(text, tokens, place)
        hits = find_phrase(street, scoped)
        if hits and place_ok:
            house = parse_house(address.number) if address.number.strip() else None
            outcomes = []
            for hit in hits:
                highlights += _spans(scoped, hit)
                after = scoped[hit[-1]].end
                listed = parse_numbers(text[after:])
                if listed.where:
                    highlights.append((after + listed.where[0], after + listed.where[1]))
                if not listed.items or not listed.reliable:
                    outcomes.append("street_open")  # the street is named, how much of it is unknown
                elif house is None:
                    outcomes.append("street_ask_number")
                else:
                    outcomes.append("street_number" if covers(listed.items, house) else "number_outside")
            reason = min(outcomes, key=_OUTCOMES.index)  # an occurrence that affects the house beats one that does not
            level = "near" if reason == "number_outside" else "likely"
        elif place_hits and not _structured(text):
            highlights += [s for hit in place_hits for s in _spans(tokens, hit)]
            level, reason = "possible", "place_in_text"
    elif place_hits:
        highlights += [s for hit in place_hits for s in _spans(tokens, hit)]
        level, reason = "likely", "place_only"
    if level is None and in_fields:
        level, reason = "area", "area"
    if level is None:
        return None
    digest = hashlib.sha1(f"{address.id}|{day}|{row.municipality}|{row.when}|{text}".encode()).hexdigest()[:16]
    return Match(level, reason, day, row, tuple(sorted(set(highlights))), digest, address.area)


def find_matches(address: Address, pages: list[Page]) -> list[Match]:
    """Everything the pages say that concerns the address, soonest first."""
    area = AREAS[address.area]
    found = [
        m
        for page in pages
        if page.day is not None
        for row in page.rows
        if (m := match_row(address, row, page.day, area.home))
    ]
    return sorted(found, key=lambda m: (m.day, m.row.start or time.min, LEVELS.index(m.level)))


# --- fetching -----------------------------------------------------------------------------------


class FetchError(Exception):
    """A page could not be read; the message is meant for the person using the site."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


Opener = Callable[[str, dict[str, str], float], Response]


def urlopen_response(url: str, headers: dict[str, str], timeout: float) -> Response:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            body = reply.read(MAX_BYTES + 1)
            return Response(reply.status, {k.lower(): v for k, v in reply.headers.items()}, body)
    except urllib.error.HTTPError as e:  # includes 304 Not Modified
        with e:
            return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, b"")


@dataclass
class _Cached:
    page: Page
    etag: str | None
    modified: str | None


@dataclass(frozen=True)
class AreaData:
    pages: tuple[Page, ...]  # whatever could be had, old pages included when a fetch failed
    checked_at: float
    errors: tuple[str, ...]


class Fetcher:
    """Reads the four day pages of an area, asking the server whether they changed instead of re-downloading."""

    def __init__(
        self,
        opener: Opener = urlopen_response,
        *,
        pause: float = 0.5,
        timeout: float = 15.0,
        now: Callable[[], float] = clock.time,
        sleep: Callable[[float], None] = clock.sleep,
    ):
        self._opener = opener
        self._pause = pause
        self._timeout = timeout
        self._now = now
        self._sleep = sleep
        self._cache: dict[str, _Cached] = {}

    def fetch(self, area_key: str, today: date) -> AreaData:
        area = AREAS[area_key]
        pages, errors = [], []
        for day in range(DAYS):
            if day:
                self._sleep(self._pause)  # be gentle with a public server
            url = area.url(day)
            try:
                page = self._get(url)
            except FetchError as e:
                errors.append(f"{area.name}, dan {day}: {e}")
                page = self._cache[url].page if url in self._cache else None
            if page is not None:
                pages.append(page if page.day else replace(page, day=today + timedelta(days=day)))
        return AreaData(tuple(pages), self._now(), tuple(errors))

    def _get(self, url: str) -> Page:
        cached = self._cache.get(url)
        headers = {"User-Agent": USER_AGENT}
        if cached and cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached and cached.modified:
            headers["If-Modified-Since"] = cached.modified
        try:
            reply = self._opener(url, headers, self._timeout)
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise FetchError(str(e) or type(e).__name__) from None
        if reply.status == 304 and cached:
            return cached.page
        if reply.status != 200:
            raise FetchError(f"HTTP {reply.status}")
        if len(reply.body) > MAX_BYTES:
            raise FetchError("odgovor je prevelik")
        try:
            page = parse_page(reply.body)
        except ParseError as e:
            raise FetchError(str(e)) from None
        self._cache[url] = _Cached(page, reply.headers.get("etag"), reply.headers.get("last-modified"))
        return page
