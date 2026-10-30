"""The pages of the personal site. Rendered on the server: no scripts, no fonts or files from elsewhere."""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from . import __version__
from .messages import LEVEL_LABELS, REASONS, day_name, day_short, window
from .outages import AREAS, DAYS, Address, AreaData, Match, Row
from .serbian import fold

TIMETABLE_LEVELS = ("likely", "possible", "near")  # what is drawn on the hour scale
TEXT_LIMIT = 800  # characters of an announcement shown in the full list, unless it concerns the address


@dataclass(frozen=True)
class AddressResult:
    address: Address
    matches: tuple[Match, ...]
    data: AreaData | None  # None until the first read of the area has finished


@dataclass(frozen=True)
class View:
    now: datetime
    results: tuple[AddressResult, ...]
    channels: str  # "Telegram, e-mail"; empty when no notification channel is set up
    interval: int  # minutes between two reads of the distributor's pages
    flash: str = ""
    error: str = ""  # why the form was refused
    form: Mapping[str, str] = field(default_factory=dict)
    suggestions: tuple[str, ...] = ()  # places the distributor mentioned, for the form
    busy: bool = False


@dataclass(frozen=True)
class Verdict:
    tone: str  # alarm, maybe, clear or unknown
    text: str
    note: str = ""


# --- what the matches add up to -----------------------------------------------------------------


def finished(match: Match, now: datetime) -> bool:
    if match.day != now.date():
        return match.day < now.date()
    return bool(match.row.end and now.time() > match.row.end)


def ongoing(match: Match, now: datetime) -> bool:
    row = match.row
    return match.day == now.date() and bool(row.start and row.end and row.start <= now.time() <= row.end)


def _when(match: Match, today: date) -> str:
    """"Sutra od 09:00 do 14:00"."""
    return f"{day_name(match.day, today).capitalize()} od {window(match)}"


def judge(result: AddressResult, now: datetime) -> Verdict:
    """One sentence answering "will there be power?" for an address."""
    data, today = result.data, now.date()
    if data is None or not data.pages:
        if data and data.errors:
            return Verdict("unknown", "Ne mogu da pročitam sajt distributera.", data.errors[0])
        return Verdict("unknown", "Čitam najave distributera…")
    note = "Poslednja provera nije uspela: " + data.errors[0] if data.errors else ""
    days_read = {p.day for p in data.pages if p.day and p.day >= today}
    if not days_read:
        return Verdict("unknown", "Podaci su stari, a sajt distributera trenutno nije dostupan.", note)

    live = [m for m in result.matches if not finished(m, now)]
    likely = [m for m in live if m.level == "likely"]
    possible = [m for m in live if m.level == "possible"]
    near = [m for m in live if m.level == "near"]
    others = sum(1 for m in result.matches if m.level == "area")

    if likely:
        first = next((m for m in likely if ongoing(m, now)), likely[0])
        if ongoing(first, now):
            until = f"{first.row.end:%H:%M}" if first.row.end else "kraja najave"
            text = f"Struje trenutno verovatno nema, do {until}."
        else:
            text = f"{_when(first, today)} verovatno nema struje."
        if len(likely) > 1:
            text += f" Ima još najava za tvoju adresu: {len(likely) - 1}."
        return Verdict("alarm", text, note)
    if possible:
        return Verdict("maybe", f"{_when(possible[0], today)} možda nema struje. Pročitaj najavu ispod.", note)
    if near:
        return Verdict(
            "maybe",
            f"Tvoja ulica je u najavi ({day_name(near[0].day, today)}), ali tvoj broj nije u navedenim opsezima.",
            note,
        )
    text = f"Nema najave za tvoju adresu u narednih {DAYS} dana."
    if len(days_read) < DAYS:
        text = f"Nema najave za tvoju adresu (pročitano {len(days_read)} od {DAYS} dana)."
    over = [m for m in result.matches if m.level == "likely" and m.day == today and finished(m, now)]
    if over:
        end = over[-1].row.end
        text = f"Isključenje je završeno{f' u {end:%H:%M}' if end else ''}. " + text.replace("Nema najave", "Nema novih najava", 1)
    if others:
        note = (note + " " if note else "") + f"U tvojoj oblasti ima drugih najava ({others}); vidi ispod."
    return Verdict("clear", text, note)


# --- small pieces -------------------------------------------------------------------------------

e = html.escape


def marked(text: str, spans: tuple[tuple[int, int], ...]) -> str:
    """The text with the matched words wrapped in <mark>."""
    out, at = [], 0
    for start, end in sorted(spans):
        if start < at:  # overlapping spans
            start = at
        if end <= start:
            continue
        out += [e(text[at:start]), f"<mark>{e(text[start:end])}</mark>"]
        at = end
    out.append(e(text[at:]))
    return "".join(out)


def _percent(t: time) -> float:
    return (t.hour * 60 + t.minute) / 1440 * 100


def _span(row: Row) -> tuple[float, float]:
    """Where an announcement sits on the 24-hour scale, as percentages. Unreadable hours mean the whole day."""
    if not (row.start and row.end):
        return 0.0, 100.0
    left = _percent(row.start)
    right = 100.0 if row.end >= time(23, 59) else _percent(row.end)
    return left, max(right - left, 1.0)


def _label(row: Row) -> str:
    """"12–14", or "08:30–14:30" when the minutes matter."""
    if not (row.start and row.end):
        return "ceo dan"
    if row.start.minute == 0 and row.end.minute == 0:
        return f"{row.start:%H}–{row.end:%H}"
    return f"{row.start:%H:%M}–{row.end:%H:%M}"


def _lanes(items: list[tuple[float, float]]) -> list[int]:
    """Lane numbers so that overlapping bars of one day do not sit on top of each other."""
    ends: list[float] = []
    lanes = []
    for left, width in items:
        lane = next((i for i, end in enumerate(ends) if end <= left), len(ends))
        if lane == len(ends):
            ends.append(0.0)
        ends[lane] = left + width
        lanes.append(lane)
    return lanes


def lamp(tone: str, extra: str = "") -> str:
    return f'<span class="lamp {tone} {extra}" aria-hidden="true"></span>'


def timetable(result: AddressResult, now: datetime) -> str:
    today = now.date()
    scale = "".join(
        f'<span style="left:{h / 24 * 100:g}%">{h}</span>' for h in range(0, 25, 3)
    )
    rows = []
    for offset in range(DAYS):
        day = today + timedelta(days=offset)
        drawn = sorted(
            (m for m in result.matches if m.day == day and m.level in TIMETABLE_LEVELS),
            key=lambda m: m.row.start or time.min,
        )
        spans = [_span(m.row) for m in drawn]
        lanes = _lanes(spans)
        bars, said = [], []
        for m, (left, width), lane in zip(drawn, spans, lanes):
            past = " past" if finished(m, now) else ""
            label = _label(m.row)
            style = f"left:{left:.3f}%;width:{width:.3f}%;--lane:{lane}"
            inside = width >= (16 if len(label) < 8 else 25)
            bars.append(f'<i class="seg {m.level}{past}" style="{style}">{label if inside else ""}</i>')
            if not inside:  # too narrow for its hours: write them beside the bar
                side = f"right:{100 - left + 0.6:.3f}%" if left + width > 70 else f"left:{left + width + 0.6:.3f}%"
                bars.append(f'<i class="cap{past}" style="{side};--lane:{lane}">{label}</i>')
            said.append(f"{window(m)}, {LEVEL_LABELS[m.level].lower()}")
        if offset == 0:
            bars.append(f'<i class="needle" style="left:{_percent(now.time()):.3f}%"></i>')
        if not drawn:
            bars.append('<i class="none">nema najave</i>')
        spoken = "; ".join(said) if said else "nema najave"
        lane_count = max(lanes, default=-1) + 1 or 1
        rows.append(
            f'<li class="day"><div class="when"><strong>{e(day_name(day, today))}</strong>'
            f"<span>{e(day_short(day))}</span></div>"
            f'<div class="track" style="--lanes:{lane_count}" aria-hidden="true">{"".join(bars)}</div>'
            f'<span class="sr">{e(day_name(day, today))}: {e(spoken)}</span></li>'
        )
    return (
        '<div class="tt" role="group" aria-label="Raspored isključenja po satima">'
        f'<div class="ruler"><span></span><div class="scale" aria-hidden="true">{scale}</div></div>'
        f'<ol class="days">{"".join(rows)}</ol></div>'
    )


def notice(match: Match, now: datetime) -> str:
    today = now.date()
    when = day_short(match.day) if (match.day - today).days > 2 else f"{day_name(match.day, today).capitalize()}, {day_short(match.day)}"
    head = f"{when} {_label(match.row)}"
    return (
        f'<li class="notice"><p class="head"><strong>{e(head)}</strong>'
        f'<span class="badge">{lamp(_TONE_OF[match.level], "small")}{e(LEVEL_LABELS[match.level])}</span>'
        f'{"<span class=badge>završeno</span>" if finished(match, now) else ""}</p>'
        f'<blockquote lang="sr-Latn">{marked(match.row.text, match.highlights)}</blockquote>'
        f'<p class="why">{e(REASONS[match.reason])} Opština: {e(_place(match.row))}.</p></li>'
    )


def _place(row: Row) -> str:
    """"Palilula - Niš"; the branch is added only when it says something the municipality does not."""
    if row.branch and fold(row.branch) not in fold(row.municipality):
        return f"{row.municipality} ({row.branch})"
    return row.municipality


_TONE_OF = {"likely": "alarm", "possible": "maybe", "near": "maybe", "area": "clear"}


def everything(result: AddressResult, now: datetime) -> str:
    """Every announcement of the area, the matched ones marked, so a wrong guess of this program can be caught."""
    area, data = AREAS[result.address.area], result.data
    if data is None:
        return ""
    items, count = [], 0
    for page in sorted((p for p in data.pages if p.day), key=lambda p: p.day):
        for row in page.rows:
            count += 1
            hit = next((m for m in result.matches if m.row is row), None)
            flag = f' <span class="badge">{lamp(_TONE_OF[hit.level], "small")}{e(LEVEL_LABELS[hit.level])}</span>' if hit else ""
            when = f"{day_short(page.day)} {row.start:%H:%M}–{row.end:%H:%M}" if row.start and row.end else f"{day_short(page.day)} {row.when}"
            place = _place(row)
            mine = hit is not None and hit.level != "area"  # an announcement of the municipality is not the person's own
            text, spans = row.text, (hit.highlights if hit else ())
            if not mine and len(text) > TEXT_LIMIT:
                text, spans = text[:TEXT_LIMIT].rstrip() + "…", ()
            items.append(
                f'<li><strong>{e(when)}</strong> {e(place)}{flag}<br>'
                f'<span class="plain" lang="sr-Latn">{marked(text, spans)}</span></li>'
            )
    if not count:
        return f'<p class="meta">Distributer za oblast {e(area.name)} trenutno nema nijednu najavu.</p>'
    return (
        f'<details class="more"><summary>Sve najave u oblasti {e(area.name)} ({count})</summary>'
        f'<ul class="all">{"".join(items)}</ul></details>'
    )


def _fresh(data: AreaData | None, now: datetime) -> str:
    if data is None:
        return ""
    at = datetime.fromtimestamp(data.checked_at, now.tzinfo)
    return f"Provereno u {at:%H:%M}." + (f" Podaci su stari: {data.errors[0]}" if data.errors else "")


def board(result: AddressResult, view: View) -> str:
    now, address = view.now, result.address
    verdict = judge(result, now)
    shown = [m for m in result.matches if m.level in TIMETABLE_LEVELS]
    area = AREAS[address.area]
    where = f"{e(address.line)}" + (f", oblast {e(area.name)}" if address.place and fold(area.name) not in fold(address.place) else "")
    notes = " ".join(filter(None, [verdict.note, _fresh(result.data, now)]))
    title = e(address.label) + f' <span class="line">{where}</span>' if address.label else where
    switch = (
        f'<form class="inline" method="post" action="/address/{e(address.id)}/notify">'
        f'<button class="link" type="submit">{"Isključi" if address.notify else "Uključi"}</button></form>'
        if view.channels
        else ""
    )
    state = "nisu podešena" if not view.channels else "uključena" if address.notify else "isključena"
    body = [
        f'<section class="board" aria-labelledby="h-{e(address.id)}">',
        f'<h2 id="h-{e(address.id)}">{lamp(verdict.tone)}{title}</h2>',
        f'<p class="verdict">{e(verdict.text)}</p>',
        f'<p class="meta">{e(notes)}</p>' if notes else "",
        # nothing is drawn while nothing is known: a row saying "nema najave" would be a claim
        timetable(result, now) if verdict.tone != "unknown" else "",
    ]
    if shown:
        body.append("<h3>Šta piše u najavama</h3>")
        body.append(f'<ul class="notices">{"".join(notice(m, now) for m in shown)}</ul>')
    body.append(everything(result, now))
    body.append(
        f'<div class="actions"><span>Obaveštenja za ovu adresu: {state}.</span>{switch}'
        f'<form class="inline" method="post" action="/address/{e(address.id)}/remove">'
        '<button class="link" type="submit">Ukloni adresu</button></form></div></section>'
    )
    return "".join(body)


def form(view: View, *, open_: bool) -> str:
    values = view.form
    chosen = values.get("area") or "ni"
    options = "".join(
        f'<option value="{k}"{" selected" if k == chosen else ""}>{e(a.name)}</option>' for k, a in AREAS.items()
    )
    places = "".join(f'<option value="{e(s)}"></option>' for s in view.suggestions)
    notify = values.get("notify", "on") == "on" if values else True
    error = f'<p class="flash error" role="alert">{e(view.error)}</p>' if view.error else ""
    inner = f"""{error}
<form method="post" action="/address" class="add">
  <div class="fields">
    <label>Oblast distributera
      <select name="area">{options}</select>
      <small>Pirot i Leskovac su u oblasti Niš, Subotica u oblasti Novi Sad, Požarevac u oblasti Kragujevac, Čačak u oblasti Kraljevo.</small></label>
    <label>Mesto
      <input name="place" value="{e(values.get("place", ""))}" list="places" maxlength="60" required autocomplete="off">
      <small>Grad, opština ili selo. Za veći grad upiši grad (Niš), ne gradsku opštinu, da ti ulica ne promakne ako pogrešiš opštinu.</small></label>
    <label>Ulica
      <input name="street" value="{e(values.get("street", ""))}" maxlength="80" autocomplete="off">
      <small>Ostavi prazno za selo bez ulica. Latinica ili ćirilica, može i bez kvačica.</small></label>
    <label>Kućni broj
      <input name="number" value="{e(values.get("number", ""))}" maxlength="12" autocomplete="off">
      <small>Treba kad najava nabraja brojeve, na primer 9-11.</small></label>
    <label>Naziv
      <input name="label" value="{e(values.get("label", ""))}" maxlength="40" autocomplete="off">
      <small>Nije obavezno, na primer Stan ili Roditelji.</small></label>
  </div>
  <p class="check"><label><input type="checkbox" name="notify"{" checked" if notify else ""}> Javi mi{" na " + e(view.channels) if view.channels else ""} čim se najavi isključenje</label></p>
  <datalist id="places">{places}</datalist>
  <button class="primary" type="submit">Sačuvaj adresu</button>
</form>"""
    if open_:
        return f'<section class="intro">{inner}</section>'
    return f'<details class="add"{" open" if view.error else ""}><summary>Dodaj još jednu adresu</summary>{inner}</details>'


def render(view: View) -> str:
    waiting = view.busy or any(r.data is None for r in view.results)
    refresh = 4 if waiting else 900  # a form being typed in is only lost after a quarter of an hour
    if view.results:
        sources = ", ".join(
            f'<a href="{e(AREAS[k].page_url)}">{e(AREAS[k].name)}</a>' for k in dict.fromkeys(r.address.area for r in view.results)
        )
    else:
        sources = '<a href="https://www.elektrodistribucija.rs">elektrodistribucija.rs</a>'
    flash = f'<p class="flash" role="status">{e(view.flash)}</p>' if view.flash else ""
    if view.results:
        main = "".join(board(r, view) for r in view.results) + form(view, open_=False)
    else:
        main = (
            '<section class="intro"><h2>Gde stanuješ?</h2>'
            f"<p>Upiši adresu prebivališta. Program će svakih {view.interval} minuta čitati najave distributera i reći ti "
            "da li se planirano isključenje struje tiče tvoje ulice, a ako podesiš Telegram ili e-mail, javiće ti i unapred.</p></section>"
            + form(view, open_=True)
        )
    notifications = (
        f"Obaveštenja idu na: {e(view.channels)}."
        if view.channels
        else "Obaveštenja nisu podešena. Dodaj sekciju [telegram] ili [email] u config.toml."
    )
    test = (
        '<form class="inline" method="post" action="/test-notification"><button class="link" type="submit">Pošalji probnu poruku</button></form>'
        if view.channels
        else ""
    )
    return f"""<!doctype html>
<html lang="sr-Latn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta http-equiv="refresh" content="{refresh}">
<link rel="icon" href="data:,">
<title>Struja: planirana isključenja</title>
<style>{CSS}</style>
</head>
<body>
<main>
<header class="bar">
  <h1 class="brand">powerout <span>planirana isključenja struje</span></h1>
  <form class="inline" method="post" action="/refresh"><button type="submit">Proveri sada</button></form>
</header>
{flash}
{main}
<footer>
  <p>{notifications} {test}</p>
  <p>Izvor: {sources}. Program poredi tekst najave sa tvojom adresom, pa zvanični sajt ostaje merodavan. Neplanirane nestanke struje distributer ne objavljuje, pa ih ova stranica ne vidi.</p>
  <p class="meta">powerout {e(__version__)}. Podaci se čitaju na svakih {view.interval} minuta; stranica se osvežava sama.</p>
</footer>
</main>
</body>
</html>
"""


CSS = """
:root{color-scheme:light dark;
--paper:#EDF0F2;--panel:#FAFBFC;--track:#E1E7EB;--ink:#1B2934;--soft:#566979;--rule:#CAD3DA;--focus:#1B5FAA;
--cut:#C41E2A;--on-cut:#fff;--maybe:#B27300;--maybe-fill:#F2B63D;--clear:#1B7A5A;--mark:#FFE08A;
--grid1:rgba(27,41,52,.07);--grid3:rgba(27,41,52,.17);
--sans:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Noto Sans","Helvetica Neue",Arial,sans-serif;
--serif:Georgia,"Iowan Old Style","Palatino Linotype","DejaVu Serif",serif}
@media (prefers-color-scheme:dark){:root{
--paper:#0E171E;--panel:#15222C;--track:#1B2B37;--ink:#E7EEF2;--soft:#9AADBB;--rule:#2A3C49;--focus:#7DB4F0;
--cut:#F0575F;--on-cut:#25080A;--maybe:#E7A93B;--maybe-fill:#C98A1C;--clear:#4FC79A;--mark:#6B5712;
--grid1:rgba(231,238,242,.07);--grid3:rgba(231,238,242,.17)}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:1rem/1.55 var(--sans)}
main{max-width:54rem;margin:0 auto;padding:0 1.25rem 3rem}
a{color:inherit;text-underline-offset:.18em}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
p{margin:.6rem 0}
.bar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.5rem 1rem;padding:1rem 0;border-bottom:1px solid var(--rule)}
.brand{margin:0;font-size:1rem;font-weight:700;letter-spacing:-.01em}
.brand span{font-weight:400;color:var(--soft);margin-left:.5rem}
.flash{margin:1rem 0 0;padding:.7rem 1rem;border-left:4px solid var(--soft);background:var(--panel)}
.flash.error{border-color:var(--cut)}
.intro{padding:2.2rem 0 .5rem}
.intro h2{margin:0 0 .4rem;font-size:clamp(1.8rem,5vw,2.6rem);line-height:1.1;letter-spacing:-.02em}
.intro p{max-width:60ch}
.board{padding:2rem 0 1.6rem;border-bottom:1px solid var(--rule)}
.board h2{margin:0;font-size:1rem;font-weight:600;display:flex;align-items:center;gap:.6rem}
.board h2 .line{font-weight:400;color:var(--soft)}
.verdict{margin:.7rem 0 .4rem;font-size:clamp(1.6rem,4.4vw,2.35rem);line-height:1.14;font-weight:650;letter-spacing:-.018em;text-wrap:balance;max-width:30ch}
.meta{margin:.2rem 0;color:var(--soft);font-size:.9rem}
.lamp{flex:none;display:inline-block;width:.9rem;height:.9rem;border-radius:50%;border:2px solid var(--tone,var(--soft))}
.lamp.small{width:.7rem;height:.7rem;margin-right:.4rem;vertical-align:-.05em}
.lamp.alarm{--tone:var(--cut);background:var(--cut)}
.lamp.maybe{--tone:var(--maybe);background:linear-gradient(90deg,var(--maybe) 50%,transparent 50%)}
.lamp.clear{--tone:var(--clear)}
.lamp.unknown{--tone:var(--soft);border-style:dotted}
.tt{margin:1.7rem 0 0}
.ruler,.day{display:grid;grid-template-columns:5.6rem 1fr;column-gap:.9rem;align-items:center}
.scale{position:relative;height:1.1rem;font-size:.75rem;color:var(--soft);font-variant-numeric:tabular-nums}
.scale span{position:absolute;top:0;transform:translateX(-50%)}
.scale span:first-child{transform:none}
.scale span:last-child{transform:translateX(-100%)}
.days{list-style:none;margin:0;padding:0}
.day{padding:.28rem 0}
.when{line-height:1.2}
.when strong{display:block;font-weight:650}
.when span{font-size:.8rem;color:var(--soft)}
.track{position:relative;height:calc(var(--lanes,1)*1.7rem + .3rem);border-radius:.3rem;overflow:hidden;
background:linear-gradient(90deg,var(--grid3) 1px,transparent 1px) 0 0/12.5% 100%,linear-gradient(90deg,var(--grid1) 1px,transparent 1px) 0 0/4.16667% 100%,var(--track)}
.seg,.cap{position:absolute;top:calc(var(--lane,0)*1.7rem + .15rem);height:1.4rem;line-height:1.4rem;font-style:normal;font-size:.78rem;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.seg{border-radius:.25rem;padding:0 .4rem;overflow:hidden}
.seg.likely{background:var(--cut);color:var(--on-cut)}
.seg.possible{background:var(--maybe-fill) repeating-linear-gradient(135deg,rgba(0,0,0,.22) 0 4px,transparent 4px 9px);color:#2A1D00}
.seg.near{border:2px dashed var(--soft);line-height:1.2rem;color:var(--soft)}
.cap{color:var(--ink)}
.past{opacity:.45}
.needle{position:absolute;top:0;bottom:0;width:2px;margin-left:-1px;background:var(--ink)}
.none{position:absolute;inset:0;display:flex;align-items:center;padding-left:.7rem;font-style:normal;font-size:.85rem;color:var(--soft)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.board h3{margin:1.9rem 0 .7rem;font-size:1rem;font-weight:650}
.notices{list-style:none;margin:0;padding:0;display:grid;gap:1.2rem}
.head{margin:0 0 .4rem;display:flex;flex-wrap:wrap;align-items:baseline;gap:.2rem .9rem}
.badge{font-size:.85rem;color:var(--soft)}
blockquote{margin:0;padding:.1rem 0 .1rem 1rem;border-left:3px solid var(--rule);font:1.04rem/1.65 var(--serif);max-width:66ch;overflow-wrap:anywhere}
mark{background:var(--mark);color:inherit;padding:0 .12em;border-radius:.15em}
.why{margin:.45rem 0 0;font-size:.9rem;color:var(--soft);max-width:66ch}
details{margin-top:1.1rem}
summary{cursor:pointer;font-weight:600;padding:.2rem 0}
.all{list-style:none;margin:.6rem 0 0;padding:0;font-size:.93rem}
.all li{padding:.5rem 0;border-top:1px solid var(--rule)}
.plain{font-family:var(--serif);color:var(--soft)}
.plain mark{color:var(--ink)}
.actions{display:flex;flex-wrap:wrap;align-items:center;gap:.3rem 1.4rem;margin-top:1.3rem;font-size:.92rem;color:var(--soft)}
form.inline{display:inline}
button{font:inherit;padding:.5rem .95rem;border:1px solid var(--ink);background:transparent;color:var(--ink);border-radius:.3rem;cursor:pointer}
button:hover{background:var(--track)}
button.primary{background:var(--ink);color:var(--paper);margin-top:.4rem}
button.link{border:0;padding:.15rem 0;background:none;text-decoration:underline;text-underline-offset:.18em;color:var(--ink)}
.add{margin-top:1.4rem}
.fields{display:grid;gap:1.1rem;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));margin-top:1rem}
label{display:grid;gap:.25rem;font-weight:600;font-size:.93rem;align-content:start}
label small{font-weight:400;color:var(--soft);font-size:.82rem;line-height:1.35}
input,select{font:inherit;font-weight:400;padding:.55rem .65rem;border:1px solid var(--soft);border-radius:.3rem;background:var(--panel);color:var(--ink);min-width:0;width:100%}
.check{margin:1rem 0 .4rem}
.check label{display:flex;gap:.55rem;align-items:center;font-weight:400}
.check input{width:auto}
footer{margin-top:2rem;font-size:.88rem;color:var(--soft);max-width:66ch}
footer .meta{font-size:.82rem}
@media (max-width:40rem){.ruler,.day{grid-template-columns:1fr;row-gap:.2rem}.ruler>span{display:none}.when strong{display:inline;margin-right:.5rem}}
"""
