"""The texts of all notifications (Serbian), kept in one place."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime, tzinfo

from .notify import Message
from .outages import AREAS, Match


def format_duration(seconds: float) -> str:
    """The two most significant units, e.g. "55 min 28 s" or "2 h 5 min"."""
    total = max(0, round(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [(days, "d"), (hours, "h"), (minutes, "min"), (secs, "s")]
    first = next((i for i, (n, _) in enumerate(parts) if n), len(parts) - 1)
    return " ".join(f"{n} {unit}" for n, unit in parts[first : first + 2] if n) or "0 s"


WEEKDAYS = ("ponedeljak", "utorak", "sreda", "četvrtak", "petak", "subota", "nedelja")
WEEKDAYS_SHORT = ("pon", "uto", "sre", "čet", "pet", "sub", "ned")

# Why an announcement was matched to an address, by Match.reason.
REASONS = {
    "street_number": "Pominje se tvoja ulica i tvoj broj je u navedenom opsegu.",
    "street_open": "Pominje se tvoja ulica. Brojevi nisu navedeni, pa može biti samo deo ulice.",
    "street_ask_number": "Pominje se tvoja ulica, a navedeni su brojevi kuća. Tvoj broj nije unet u adresu, pa proveri da li je među njima.",
    "number_outside": "Ulica je na spisku, ali tvoj broj nije u navedenim opsezima. Opseg poput 9-11 važi za jednu stranu ulice (9 i 11).",
    "place_in_text": "Pominje se tvoje mesto, ali ne i tvoja ulica. Tekst ne nabraja brojeve, pa pročitaj najavu.",
    "place_only": "Pominje se tvoje mesto.",
    "area": "Isključenje je u tvojoj oblasti; tvoja ulica se ne pominje.",
}
LEVEL_LABELS = {
    "likely": "Verovatno te pogađa",
    "possible": "Moguće, proveri",
    "near": "Skoro pogodak",
    "area": "U tvojoj oblasti",
}


def day_name(day: date, today: date) -> str:
    """"danas", "sutra", "prekosutra", or the weekday."""
    return {0: "danas", 1: "sutra", 2: "prekosutra"}.get((day - today).days) or WEEKDAYS[day.weekday()]


def day_short(day: date) -> str:
    return f"{WEEKDAYS_SHORT[day.weekday()]} {day.day}.{day.month}."


def window(match: Match) -> str:
    """"09:00 do 14:00" as far as the page could be read, else the cell as published."""
    row = match.row
    if row.start and row.end:
        return f"{row.start:%H:%M} do {row.end:%H:%M}"
    return row.when


class Formatter:
    """Builds the notifications; every timestamp is shown in the configured time zone."""

    def __init__(self, tz: tzinfo | None = None, clock: Callable[[], float] = time.time):
        self._tz = tz
        self._clock = clock

    def time(self, ts: float) -> str:
        """HH:MM:SS, with the date too unless it is today."""
        moment = datetime.fromtimestamp(ts, self._tz)
        today = datetime.fromtimestamp(self._clock(), self._tz).date()
        return moment.strftime("%H:%M:%S" if moment.date() == today else "%d.%m. %H:%M:%S")

    def power_lost(self, name: str, at: float) -> Message:
        return Message(
            f"⚡ Nestala struja: {name}",
            f"Struja je nestala u {self.time(at)}. Uređaj sada radi na rezervnom napajanju (baterija/UPS).",
        )

    def power_restored(self, name: str, at: float, duration: float) -> Message:
        return Message(
            f"💡 Struja se vratila: {name}",
            f"Struja se vratila u {self.time(at)}. Nije je bilo {format_duration(duration)}.",
        )

    def unexpected_restart(self, name: str, since: float, until: float) -> Message:
        return Message(
            f"🔌 Uređaj je bio ugašen: {name}",
            "Uređaj je ponovo pokrenut posle neočekivanog prekida rada. "
            f"Poslednji put je radio oko {self.time(since)}, a ponovo je počeo u {self.time(until)} "
            f"(prekid je trajao oko {format_duration(until - since)}). Verovatan uzrok je nestanak struje.",
        )

    def restart_after_outage(self, name: str, since: float, until: float) -> Message:
        return Message(
            f"🔌 Uređaj je ponovo pokrenut: {name}",
            f"Uređaj se ugasio za vreme nestanka struje (oko {self.time(since)}) i ponovo je pokrenut u "
            f"{self.time(until)}; nije radio oko {format_duration(until - since)}. Struja je verovatno ponovo tu.",
        )

    def signal_lost(self, name: str, last_seen: float, power: str, now: float) -> Message:
        cause = (
            "Uređaj je tada javio da radi na bateriji, pa je verovatno nestala struja."
            if power == "battery"
            else "Uzrok može biti nestanak struje, pad interneta ili gašenje uređaja."
        )
        return Message(
            f"⚠️ Nema signala: {name}",
            f"Poslednji signal je stigao u {self.time(last_seen)} (pre {format_duration(now - last_seen)}). {cause}",
        )

    def signal_restored(self, name: str, last_seen: float, now: float) -> Message:
        return Message(
            f"✅ Signal je ponovo tu: {name}",
            f"Uređaj se ponovo javio u {self.time(now)}. Nije se javljao {format_duration(now - last_seen)}.",
        )

    def test(self, name: str) -> Message:
        return Message(
            f"🧪 Probna poruka: {name}",
            "Obaveštenja rade. Ovako će izgledati poruka kada nestane struja.",
        )

    def delayed(self, message: Message, delay: float) -> Message:
        """The message with a note that it could not be sent right away."""
        note = f"ℹ️ Poruka stiže sa zakašnjenjem od {format_duration(delay)} (nije bilo veze sa internetom)."
        return Message(message.title, f"{message.body}\n\n{note}" if message.body else note)

    def planned_outage(self, title: str, where: str, match: Match, today: date, *, reminder: bool = False) -> Message:
        """The notice about an announced cut that concerns an address; `title` names it, `where` is its street."""
        sure = match.level == "likely"
        verdict = "verovatno nema struje" if sure else "možda nema struje"
        if reminder:
            head = f"⏰ Danas {'nema' if sure else 'možda nema'} struje: {title}"
        else:
            head = f"{'⚡ Najavljeno' if sure else '❓ Moguće'} isključenje struje: {title}"
        day = f"{day_name(match.day, today).capitalize()} ({match.day.day}.{match.day.month}.)"
        quote = match.row.text if len(match.row.text) <= 500 else match.row.text[:500].rstrip() + "…"
        return Message(
            head,
            f"{day} od {window(match)} {verdict} na adresi {where}.\n"
            f"{REASONS[match.reason]}\n\n"
            f"Tekst najave ({match.row.municipality}): «{quote}»\n"
            f"Izvor: {AREAS[match.area].page_url}",
        )
