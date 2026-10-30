import http.client
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from powerout.config import Config, ConfigError
from powerout.messages import Formatter
from powerout.notify import Attempt
from powerout.outages import AREAS, Address, AreaData
from powerout.site import (
    KEEP_SENT,
    MAX_ADDRESSES,
    AddressBook,
    AddressError,
    Poller,
    SentLog,
    Site,
    SiteServer,
    run_site,
)
from powerout.store import JsonStore
from tests.helpers import Clock, wait_until
from tests.test_pages import CHECKED, TZ, page, row

NOW = datetime(2026, 10, 30, 11, 0, tzinfo=TZ).timestamp()


def at(day, hour, minute=0) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp()


def form(**overrides) -> dict[str, str]:
    values = {"area": "ni", "place": "Niš", "street": "Trebinjska", "number": "12", "label": "Stan", "notify": "on"}
    return {**values, **overrides}


class TempState(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def book(self) -> AddressBook:
        return AddressBook(JsonStore(self.dir / "addresses.json"))


class AddressBookTest(TempState):
    def test_an_address_is_saved_and_survives_a_restart(self):
        book = self.book()
        address = book.add(form())
        again = self.book().all()
        self.assertEqual(again, [address])
        self.assertEqual((address.area, address.place, address.street, address.number, address.label, address.notify), ("ni", "Niš", "Trebinjska", "12", "Stan", True))

    def test_text_is_tidied_and_the_checkbox_is_read(self):
        address = self.book().add(form(place="  Niš  ", street="Bulevar\n  Nemanjića", notify=""))
        self.assertEqual((address.place, address.street, address.notify), ("Niš", "Bulevar Nemanjića", False))

    def test_street_number_and_label_are_optional(self):
        address = self.book().add({"area": "ni", "place": "Pukovac"})
        self.assertEqual((address.street, address.number, address.label), ("", "", ""))

    def test_refusals_name_the_field(self):
        cases = [
            (form(area="xx"), "oblast"),
            (form(area=""), "oblast"),
            (form(place=""), "Mesto"),
            (form(place="   "), "Mesto"),
            (form(place="x" * 61), "Mesto"),
            (form(street="x" * 81), "Ulica"),
            (form(number="1234567890123"), "Kućni broj"),
            (form(label="x" * 41), "Naziv"),
            (form(place="Niš\x00"), "Mesto"),
            (form(street="Ulica‮"), "Ulica"),
        ]
        for fields, fragment in cases:
            with self.subTest(fragment=fragment, fields=fields):
                book = self.book()
                with self.assertRaises(AddressError) as caught:
                    book.add(fields)
                self.assertIn(fragment, str(caught.exception))
                self.assertEqual(book.all(), [])

    def test_the_same_address_twice_is_refused_however_it_is_written(self):
        book = self.book()
        book.add(form(place="Niš", street="Trebinjska", number="12"))
        for variant in (form(place="NIS", street="trebinjska", number="12"), form(place="Ниш", street="Требињска", number="12")):
            with self.subTest(variant=variant):
                with self.assertRaises(AddressError) as caught:
                    book.add(variant)
                self.assertIn("već sačuvana", str(caught.exception))
        book.add(form(number="14"))  # another house is another address
        book.add(form(area="ns"))  # and so is another area
        self.assertEqual(len(book.all()), 3)

    def test_there_is_a_limit(self):
        book = self.book()
        for n in range(MAX_ADDRESSES):
            book.add(form(number=str(n + 1)))
        with self.assertRaises(AddressError) as caught:
            book.add(form(number="99"))
        self.assertIn(str(MAX_ADDRESSES), str(caught.exception))

    def test_ids_are_unique(self):
        book = self.book()
        ids = {book.add(form(number=str(n))).id for n in range(5)}
        self.assertEqual(len(ids), 5)

    def test_remove(self):
        book = self.book()
        keep, drop = book.add(form(number="1")), book.add(form(number="2"))
        self.assertTrue(book.remove(drop.id))
        self.assertFalse(book.remove(drop.id))
        self.assertEqual(self.book().all(), [keep])

    def test_notifications_can_be_switched_per_address(self):
        book = self.book()
        address = book.add(form())
        self.assertIs(book.toggle_notify(address.id), False)
        self.assertFalse(self.book().all()[0].notify)
        self.assertIs(book.toggle_notify(address.id), True)
        self.assertIsNone(book.toggle_notify("nope"))

    def test_damaged_entries_are_skipped_not_fatal(self):
        JsonStore(self.dir / "addresses.json").save(
            {
                "addresses": [
                    {"id": "a", "area": "ni", "place": "Niš", "street": "Trebinjska"},
                    {"id": "b", "area": "zz", "place": "Niš"},
                    {"id": "c", "area": "ni"},
                    {"area": "ni", "place": "Niš"},
                    {"id": "d", "area": "ni", "place": "Pirot", "unknown_future_field": 1},
                    "garbage",
                    None,
                ]
            }
        )
        self.assertEqual([a.id for a in self.book().all()], ["a", "d"])

    def test_a_missing_or_broken_file_is_an_empty_book(self):
        self.assertEqual(self.book().all(), [])
        (self.dir / "addresses.json").write_text("{ not json")
        with self.assertLogs("powerout.store", "WARNING"):
            self.assertEqual(self.book().all(), [])


class SentLogTest(TempState):
    def log(self) -> SentLog:
        return SentLog(JsonStore(self.dir / "sent.json"))

    def test_an_announcement_is_remembered_across_restarts(self):
        self.log().mark("k1", "announced", 1000.0)
        entry = self.log().get("k1")
        self.assertEqual(entry, {"announced": 1000.0, "reminded": None})
        self.assertIsNone(self.log().get("other"))

    def test_a_reminder_is_recorded_next_to_it(self):
        log = self.log()
        log.mark("k1", "announced", 1000.0)
        log.mark("k1", "reminded", 5000.0)
        self.assertEqual(self.log().get("k1"), {"announced": 1000.0, "reminded": 5000.0})

    def test_old_records_are_forgotten(self):
        log = self.log()
        log.mark("old", "announced", 1000.0)
        log.mark("new", "announced", 1000.0 + KEEP_SENT + 1)
        self.assertIsNone(self.log().get("old"))
        self.assertIsNotNone(self.log().get("new"))

    def test_damaged_records_are_ignored(self):
        JsonStore(self.dir / "sent.json").save({"sent": {"a": {"announced": 1.0, "reminded": None}, "b": "x", "c": {"reminded": 2.0}}})
        log = self.log()
        self.assertIsNotNone(log.get("a"))
        self.assertIsNone(log.get("b"))
        self.assertIsNone(log.get("c"))


class FakeFetcher:
    def __init__(self, **by_area: AreaData):
        self.by_area = by_area
        self.calls: list[str] = []

    def fetch(self, area, today):
        self.calls.append(area)
        return self.by_area[area]


class FakeNotifier:
    def __init__(self, ok=True, errors=None):
        self.ok = ok
        self.sent = []
        self.errors = errors or {}
        self.channels = [SimpleNamespace(name="telegram"), SimpleNamespace(name="email")]

    def send(self, message):
        self.sent.append(message)
        return self.ok

    def deliver(self, message, delivery=None):
        self.sent.append(message)
        return [Attempt(c.name, self.errors.get(c.name)) for c in self.channels]


def area_data(*day_rows, errors=()) -> AreaData:
    """Announcements as (day offset, Row) pairs; the four days are always present."""
    pages = [page(d, *[r for off, r in day_rows if off == d]) for d in range(4)]
    return AreaData(tuple(pages), CHECKED, tuple(errors))


class PollerTest(TempState):
    def setUp(self):
        super().setUp()
        self.clock = Clock(NOW)
        self.book_ = self.book()
        self.sent = SentLog(JsonStore(self.dir / "sent.json"))
        self.notifier = FakeNotifier()

    def poller(self, fetcher, notifier="default") -> Poller:
        return Poller(
            fetcher=fetcher,
            book=self.book_,
            sent=self.sent,
            notifier=self.notifier if notifier == "default" else notifier,
            formatter=Formatter(TZ, self.clock),
            tz=TZ,
            interval=1800,
            clock=self.clock,
        )

    def titles(self):
        return [m.title for m in self.notifier.sent]

    def test_only_the_areas_with_an_address_are_read(self):
        self.book_.add(form())
        fetcher = FakeFetcher(ni=area_data(), bg=area_data())
        poller = self.poller(fetcher)
        poller.refresh()
        self.assertEqual(fetcher.calls, ["ni"])
        self.assertEqual(set(poller.snapshot()), {"ni"})

    def test_with_no_address_nothing_is_read(self):
        fetcher = FakeFetcher()
        self.poller(fetcher).refresh()
        self.assertEqual(fetcher.calls, [])

    def test_an_area_is_read_once_for_all_its_addresses(self):
        self.book_.add(form(number="1"))
        self.book_.add(form(number="2"))
        fetcher = FakeFetcher(ni=area_data())
        self.poller(fetcher).refresh()
        self.assertEqual(fetcher.calls, ["ni"])

    def test_errors_of_the_read_are_logged(self):
        self.book_.add(form())
        poller = self.poller(FakeFetcher(ni=area_data(errors=("Niš, dan 2: HTTP 500",))))
        with self.assertLogs("powerout.site", "WARNING") as logs:
            poller.refresh()
        self.assertIn("HTTP 500", logs.output[0])
        self.assertFalse(poller.busy)

    def test_a_new_announcement_is_sent_once(self):
        self.book_.add(form())
        poller = self.poller(FakeFetcher(ni=area_data((1, row()))))
        poller.refresh()
        poller.refresh()
        self.assertEqual(self.titles(), ["⚡ Najavljeno isključenje struje: Stan"])
        body = self.notifier.sent[0].body
        self.assertIn("Sutra (31.10.) od 09:00 do 14:00 verovatno nema struje na adresi Trebinjska 12, Niš.", body)
        self.assertIn("delovi ulica Trebinjske", body)
        self.assertIn("https://www.elektrodistribucija.rs/planirana-iskljucenja/planirana-ni", body)

    def test_a_failed_send_is_tried_again_at_the_next_read(self):
        self.book_.add(form())
        self.notifier.ok = False
        poller = self.poller(FakeFetcher(ni=area_data((1, row()))))
        poller.refresh()
        self.assertEqual(len(self.notifier.sent), 1)
        self.notifier.ok = True
        poller.refresh()
        self.assertEqual(len(self.notifier.sent), 2)
        poller.refresh()
        self.assertEqual(len(self.notifier.sent), 2)  # delivered, so not again

    def test_a_possible_match_is_sent_with_its_own_wording(self):
        self.book_.add(form(place="Čenej", street="Neka ulica", number=""))
        r = row("Čenej: Od TENENA prema centru", municipality="Novi Sad", branch="Novi Sad")
        self.poller(FakeFetcher(ni=area_data((1, r)))).refresh()
        self.assertEqual(self.titles(), ["❓ Moguće isključenje struje: Stan"])
        self.assertIn("možda nema struje", self.notifier.sent[0].body)

    def test_near_misses_and_the_rest_of_the_area_are_not_sent(self):
        self.book_.add(form(street="Kralja Petra", number="10", place="Stari grad"))
        near = row("KRALJA PETRA: 9-11,", municipality="Stari grad", branch="")
        elsewhere = row("deo Pukovca", municipality="Doljevac")
        self.poller(FakeFetcher(ni=area_data((1, near), (1, elsewhere)))).refresh()
        self.assertEqual(self.notifier.sent, [])

    def test_an_address_with_notifications_off_is_never_sent_to(self):
        self.book_.add(form(notify=""))
        self.poller(FakeFetcher(ni=area_data((1, row())))).refresh()
        self.assertEqual(self.notifier.sent, [])

    def test_without_a_channel_the_data_is_still_read(self):
        self.book_.add(form())
        poller = self.poller(FakeFetcher(ni=area_data((1, row()))), notifier=None)
        poller.refresh()
        self.assertEqual(set(poller.snapshot()), {"ni"})

    def test_a_cut_that_is_already_over_is_not_announced(self):
        self.book_.add(form())
        self.clock.now = at(30, 15, 0)
        self.poller(FakeFetcher(ni=area_data((0, row(start=(9, 0), end=(14, 0)))))).refresh()
        self.assertEqual(self.notifier.sent, [])

    def test_each_address_gets_its_own_notice(self):
        self.book_.add(form(label="Stan", number="12"))
        self.book_.add(form(label="Roditelji", number="14"))
        self.poller(FakeFetcher(ni=area_data((1, row())))).refresh()
        self.assertEqual(sorted(self.titles()), ["⚡ Najavljeno isključenje struje: Roditelji", "⚡ Najavljeno isključenje struje: Stan"])

    def test_a_reminder_comes_on_the_morning_of_the_day_and_only_once(self):
        self.book_.add(form())
        data = FakeFetcher(ni=area_data((0, row(start=(15, 0), end=(17, 0))), (1, row(start=(15, 0), end=(17, 0)))))
        poller = self.poller(data)
        self.clock.now = at(29, 18, 0)  # announced the evening before... for tomorrow, which is "today" below
        poller.refresh()
        self.assertEqual(len(self.notifier.sent), 2)  # both announcements are new
        self.notifier.sent.clear()

        self.clock.now = at(30, 5, 0)  # small hours: the pages are republished, no reminder yet
        poller.refresh()
        self.assertEqual(self.notifier.sent, [])
        self.clock.now = at(30, 7, 30)
        poller.refresh()
        self.assertEqual(self.titles(), ["⏰ Danas nema struje: Stan"])
        self.assertIn("Danas (30.10.) od 15:00 do 17:00", self.notifier.sent[0].body)
        poller.refresh()
        self.assertEqual(len(self.notifier.sent), 1)  # once

    def test_a_cut_announced_today_needs_no_reminder(self):
        self.book_.add(form())
        self.clock.now = at(30, 8, 0)
        poller = self.poller(FakeFetcher(ni=area_data((0, row(start=(15, 0), end=(17, 0))))))
        poller.refresh()
        self.clock.now = at(30, 12, 0)
        poller.refresh()
        self.assertEqual(self.titles(), ["⚡ Najavljeno isključenje struje: Stan"])
        self.assertIn("Danas (30.10.)", self.notifier.sent[0].body)

    def test_a_changed_announcement_is_a_new_one(self):
        self.book_.add(form())
        fetcher = FakeFetcher(ni=area_data((1, row())))
        poller = self.poller(fetcher)
        poller.refresh()
        fetcher.by_area["ni"] = area_data((1, row("delovi ulica Trebinjske i Kosmajske")))
        poller.refresh()
        self.assertEqual(len(self.notifier.sent), 2)

    def test_what_was_sent_is_remembered_across_restarts(self):
        self.book_.add(form())
        fetcher = FakeFetcher(ni=area_data((1, row())))
        self.poller(fetcher).refresh()
        again = Poller(
            fetcher=fetcher,
            book=self.book(),
            sent=SentLog(JsonStore(self.dir / "sent.json")),
            notifier=self.notifier,
            formatter=Formatter(TZ, self.clock),
            tz=TZ,
            interval=1800,
            clock=self.clock,
        )
        again.refresh()
        self.assertEqual(len(self.notifier.sent), 1)

    def test_asking_for_a_read_is_rate_limited_but_a_new_address_is_not(self):
        poller = self.poller(FakeFetcher())
        self.assertTrue(poller.request_refresh())
        self.assertFalse(poller.request_refresh())
        self.assertTrue(poller.request_refresh(force=True))
        self.clock.advance(61)
        self.assertTrue(poller.request_refresh())

    def test_the_loop_reads_at_once_wakes_on_request_and_stops(self):
        self.book_.add(form())
        fetcher = FakeFetcher(ni=area_data())
        poller = self.poller(fetcher)
        thread = threading.Thread(target=poller.run, daemon=True)
        thread.start()
        self.assertTrue(wait_until(lambda: fetcher.calls == ["ni"]))
        poller.request_refresh(force=True)
        self.assertTrue(wait_until(lambda: len(fetcher.calls) == 2))
        poller.stop()
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_a_failing_read_does_not_end_the_loop(self):
        self.book_.add(form())

        class Broken:
            calls = 0

            def fetch(self, area, today):
                Broken.calls += 1
                if Broken.calls == 1:
                    raise RuntimeError("boom")
                return area_data()

        poller = self.poller(Broken())
        thread = threading.Thread(target=poller.run, daemon=True)
        with self.assertLogs("powerout.site", "ERROR"):
            thread.start()
            self.assertTrue(wait_until(lambda: Broken.calls >= 1))
            poller.request_refresh(force=True)
            self.assertTrue(wait_until(lambda: Broken.calls >= 2))
        poller.stop()
        thread.join(2)


class FakePoller:
    def __init__(self, snapshot=None):
        self._snapshot = snapshot or {}
        self.busy = False
        self.requests: list[bool] = []
        self.accept = True

    def snapshot(self):
        return dict(self._snapshot)

    def now(self):
        return datetime.fromtimestamp(NOW, TZ)

    def request_refresh(self, *, force=False):
        self.requests.append(force)
        return self.accept or force


class SiteTest(TempState):
    def make(self, snapshot=None, notifier=None):
        self.poller_ = FakePoller(snapshot)
        self.notifier = notifier
        return Site(
            book=self.book(),
            poller=self.poller_,
            notifier=notifier,
            formatter=Formatter(TZ, lambda: NOW),
            interval_minutes=30,
            name="Probni",
        )

    def test_the_view_has_a_result_for_every_address_even_before_data_arrives(self):
        site = self.make()
        site.add(form())
        view = site.view()
        self.assertEqual(len(view.results), 1)
        self.assertIsNone(view.results[0].data)
        self.assertEqual((view.interval, view.channels), (30, ""))

    def test_the_view_matches_the_address_against_its_area(self):
        site = self.make({"ni": area_data((1, row()))})
        site.add(form())
        result = site.view().results[0]
        self.assertEqual([m.level for m in result.matches if m.level != "area"], ["likely"])

    def test_places_for_the_form_come_from_the_announcements_and_the_areas(self):
        site = self.make({"ni": area_data((0, row("deo Pukovca", municipality="Doljevac")))})
        places = site.view().suggestions
        self.assertIn("Doljevac", places)
        self.assertIn("Niš", places)
        self.assertIn("Beograd", places)
        self.assertEqual(list(places), sorted(places, key=lambda p: p.lower().replace("š", "s")))

    def test_channels_are_named_for_a_person(self):
        self.assertEqual(self.make(notifier=FakeNotifier()).view().channels, "Telegram i e-mail")

    def test_a_flash_message_is_shown_once(self):
        site = self.make()
        site.add(form(label="Stan"))
        self.assertIn("Sačuvano: Stan", site.view().flash)
        self.assertEqual(site.view().flash, "")

    def test_adding_asks_for_a_read_even_if_one_was_just_asked_for(self):
        site = self.make()
        site.add(form())
        self.assertEqual(self.poller_.requests, [True])

    def test_a_refused_address_changes_nothing(self):
        site = self.make()
        with self.assertRaises(AddressError):
            site.add(form(place=""))
        self.assertEqual(site.view().results, ())
        self.assertEqual(self.poller_.requests, [])

    def test_removing_and_switching(self):
        site = self.make(notifier=FakeNotifier())
        site.add(form())
        address_id = site.view().results[0].address.id
        site.toggle(address_id)
        self.assertEqual(site.view().flash, "Obaveštenja su isključena.")
        site.toggle(address_id)
        self.assertEqual(site.view().flash, "Obaveštenja su uključena.")
        site.toggle("nope")
        self.assertEqual(site.view().flash, "Te adrese nema.")
        site.remove(address_id)
        self.assertEqual(site.view().flash, "Adresa je uklonjena.")
        site.remove(address_id)
        self.assertEqual(site.view().flash, "Te adrese nema.")

    def test_pressing_refresh_says_whether_it_was_accepted(self):
        site = self.make()
        site.refresh()
        self.assertIn("Čitam najave", site.view().flash)
        self.poller_.accept = False
        site.refresh()
        self.assertIn("upravo pročitane", site.view().flash)

    def test_the_test_message_without_a_channel_explains_what_to_do(self):
        site = self.make()
        site.test_notification()
        self.assertIn("[telegram]", site.view().flash)

    def test_the_error_of_a_telegram_chat_does_not_repeat_the_channel_name(self):
        notifier = FakeNotifier(errors={"telegram": "chat 123: telegram: HTTP 401: Unauthorized (proveri token)"})
        site = self.make(notifier=notifier)
        site.test_notification()
        self.assertIn("Telegram ne radi (chat 123: HTTP 401: Unauthorized (proveri token))", site.view().flash)

    def test_the_test_message_reports_each_channel(self):
        notifier = FakeNotifier(errors={"email": "email: SMTPAuthenticationError: bad login"})
        site = self.make(notifier=notifier)
        site.test_notification()
        self.assertEqual(site.view().flash, "Probna poruka: Telegram radi; e-mail ne radi (SMTPAuthenticationError: bad login).")
        self.assertIn("Probna poruka: Probni", notifier.sent[0].title)


class Connection:
    """A client that does not follow redirects, so that they can be inspected."""

    def __init__(self, port):
        self.port = port

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
            if body is not None and "Content-Length" not in h:
                h["Content-Length"] = str(len(body))
            conn.request(method, path, body=body, headers=h)
            reply = conn.getresponse()
            return reply.status, {k.lower(): v for k, v in reply.getheaders()}, reply.read().decode("utf-8", "replace")
        finally:
            conn.close()

    def post(self, path, fields=None, *, origin="same", **kwargs):
        from urllib.parse import urlencode

        headers = dict(kwargs.pop("headers", {}))
        if origin == "same":
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        elif origin:
            headers["Origin"] = origin
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        return self.request("POST", path, urlencode(fields or {}).encode(), headers)


class ServerTest(TempState):
    host = "127.0.0.1"

    def setUp(self):
        super().setUp()
        self.poller = FakePoller({"ni": area_data((1, row()))})
        self.notifier = FakeNotifier()
        self.site = Site(
            book=self.book(),
            poller=self.poller,
            notifier=self.notifier,
            formatter=Formatter(TZ, lambda: NOW),
            interval_minutes=30,
            name="Probni",
        )
        self.server = SiteServer((self.host, 0), self.site)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.stop)
        self.http = Connection(self.server.server_address[1])

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def test_the_page_is_served_with_protective_headers(self):
        status, headers, body = self.http.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["referrer-policy"], "same-origin")
        self.assertEqual(headers["server"], "powerout")
        self.assertIn("Gde stanuješ?", body)

    def test_health_and_unknown_paths(self):
        self.assertEqual(self.http.request("GET", "/health")[::2], (200, "ok\n"))
        self.assertEqual(self.http.request("GET", "/nope")[0], 404)
        self.assertEqual(self.http.request("GET", "/address")[0], 404)  # forms are POST-only

    def test_a_request_for_a_name_that_is_not_ours_is_refused(self):
        self.assertEqual(self.http.request("GET", "/", headers={"Host": "evil.example.com"})[0], 400)
        self.assertEqual(self.http.request("GET", "/", headers={"Host": "evil.example.com:80"})[0], 400)
        for ours in ("localhost", f"localhost:{self.http.port}", "127.0.0.1", f"[::1]:{self.http.port}"):
            with self.subTest(host=ours):
                self.assertEqual(self.http.request("GET", "/", headers={"Host": ours})[0], 200)

    def test_adding_an_address(self):
        status, headers, _ = self.http.post("/address", form())
        self.assertEqual((status, headers["location"]), (303, "/"))
        self.assertEqual([a.place for a in self.site.book.all()], ["Niš"])
        self.assertIn("Sačuvano: Stan", self.http.request("GET", "/")[2])
        self.assertNotIn("Sačuvano: Stan", self.http.request("GET", "/")[2])  # said once
        self.assertEqual(self.poller.requests, [True])

    def test_a_refused_address_is_shown_again_with_the_reason(self):
        status, _, body = self.http.post("/address", form(place="", street="Nišava"))
        self.assertEqual(status, 422)
        self.assertIn("Mesto: ovo polje je obavezno.", body)
        self.assertIn('value="Nišava"', body)
        self.assertEqual(self.site.book.all(), [])

    def test_a_form_from_another_website_changes_nothing(self):
        for origin in (None, "http://evil.example.com", "null", f"http://127.0.0.1.evil.example.com:{self.http.port}"):
            with self.subTest(origin=origin):
                self.assertEqual(self.http.post("/address", form(), origin=origin)[0], 403)
        self.assertEqual(self.site.book.all(), [])

    def post_as(self, headers, path="/refresh"):
        return self.http.request("POST", path, b"", {"Content-Type": "application/x-www-form-urlencoded", **headers})[0]

    def test_a_browser_that_says_the_request_is_from_this_page_is_believed_whatever_the_origin_says(self):
        # measured in Chrome: under "Referrer-Policy: no-referrer" a form post carries "Origin: null" and no Referer
        self.assertEqual(self.post_as({"Sec-Fetch-Site": "same-origin", "Origin": "null"}), 303)
        self.assertEqual(self.post_as({"Sec-Fetch-Site": "same-origin"}), 303)

    def test_any_other_fetch_site_is_refused_even_with_an_origin_that_matches(self):
        matching = f"http://127.0.0.1:{self.http.port}"
        for site in ("cross-site", "same-site", "none", ""):
            with self.subTest(site=site):
                self.assertEqual(self.post_as({"Sec-Fetch-Site": site, "Origin": matching}), 403)
        self.assertEqual(self.poller.requests, [])

    def test_the_referrer_policy_lets_the_pages_own_forms_prove_where_they_come_from(self):
        # Without Sec-Fetch-Site (older browsers) the check relies on Origin/Referer, and what a browser sends
        # of them depends on the policy the page declared. Measured in Chrome:
        #   no-referrer -> "Origin: null", no Referer      same-origin -> the real Origin and a Referer
        policy = self.http.request("GET", "/")[1]["referrer-policy"]
        origin = f"http://127.0.0.1:{self.http.port}"
        sent = {"no-referrer": {"Origin": "null"}}.get(policy, {"Origin": origin, "Referer": origin + "/"})
        self.assertEqual(self.post_as(sent), 303, f"a form of the page itself is refused under Referrer-Policy: {policy}")
        self.assertNotEqual(policy, "no-referrer")

    def test_a_page_of_another_site_still_gets_no_referrer(self):
        self.assertEqual(self.http.request("GET", "/")[1]["referrer-policy"], "same-origin")

    def test_the_referer_is_good_enough_when_there_is_no_origin(self):
        status = self.http.post("/address", form(), origin=None, headers={"Referer": f"http://127.0.0.1:{self.http.port}/"})[0]
        self.assertEqual(status, 303)

    def test_every_action_is_checked_for_origin(self):
        address = self.site.book.add(form())
        for path in (f"/address/{address.id}/remove", f"/address/{address.id}/notify", "/refresh", "/test-notification"):
            with self.subTest(path=path):
                self.assertEqual(self.http.post(path, origin=None)[0], 403)
        self.assertEqual(len(self.site.book.all()), 1)
        self.assertEqual(self.notifier.sent, [])

    def test_remove_switch_refresh_and_test(self):
        address = self.site.book.add(form())
        self.assertEqual(self.http.post(f"/address/{address.id}/notify")[0], 303)
        self.assertFalse(self.site.book.all()[0].notify)
        self.assertEqual(self.http.post("/refresh")[0], 303)
        self.assertEqual(self.poller.requests, [False])
        self.assertEqual(self.http.post("/test-notification")[0], 303)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.http.post(f"/address/{address.id}/remove")[0], 303)
        self.assertEqual(self.site.book.all(), [])

    def test_an_unknown_action_is_not_found(self):
        self.assertEqual(self.http.post("/address/abc/delete")[0], 404)
        self.assertEqual(self.http.post("/something")[0], 404)

    def test_a_body_that_is_too_big_or_senseless_is_refused(self):
        big = self.http.request("POST", "/address", b"x" * 9000, {"Origin": f"http://127.0.0.1:{self.http.port}"})
        self.assertEqual(big[0], 400)
        for length in ("-5", "abc"):
            with self.subTest(length=length):
                status = self.http.request("POST", "/refresh", b"", {"Origin": f"http://127.0.0.1:{self.http.port}", "Content-Length": length})[0]
                self.assertEqual(status, 400)

    def test_an_address_typed_with_markup_cannot_run_in_the_page(self):
        self.http.post("/address", form(place="<script>alert(1)</script>", label="<img src=x onerror=alert(1)>"))
        body = self.http.request("GET", "/")[2]
        self.assertNotIn("<script>alert", body)
        self.assertNotIn("<img src=x", body)


class WildcardHostTest(TempState):
    def test_listening_on_every_interface_still_refuses_foreign_forms(self):
        poller = FakePoller()
        site = Site(book=self.book(), poller=poller, notifier=None, formatter=Formatter(TZ, lambda: NOW), interval_minutes=30, name="x")
        server = SiteServer(("0.0.0.0", 0), site)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        http = Connection(server.server_address[1])
        self.assertEqual(http.request("GET", "/", headers={"Host": "192.168.1.20:8750"})[0], 200)
        self.assertEqual(http.post("/address", form(), origin="http://evil.example.com", headers={"Host": "192.168.1.20:8750"})[0], 403)
        self.assertEqual(http.post("/address", form(), origin="http://192.168.1.20:8750", headers={"Host": "192.168.1.20:8750"})[0], 303)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RunSiteTest(TempState):
    def config(self, port) -> Config:
        return Config.from_dict({"general": {"state_dir": str(self.dir / "state")}, "site": {"port": port}})

    def test_it_serves_until_told_to_stop_and_frees_the_port(self):
        port = free_port()
        stop = threading.Event()
        thread = threading.Thread(target=run_site, args=(self.config(port), stop), daemon=True)
        thread.start()
        self.assertTrue(wait_until(lambda: self._up(port)))
        stop.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self._up(port))  # nobody answers any more

    def _up(self, port) -> bool:
        try:
            return Connection(port).request("GET", "/health")[0] == 200
        except OSError:
            return False

    def test_a_port_that_is_taken_is_reported_clearly(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            with self.assertRaises(ConfigError) as caught:
                run_site(self.config(port), threading.Event())
        self.assertIn(str(port), str(caught.exception))

    def test_addresses_survive_a_restart_of_the_site(self):
        for _ in range(2):
            port = free_port()
            stop = threading.Event()
            thread = threading.Thread(target=run_site, args=(self.config(port), stop), daemon=True)
            thread.start()
            self.assertTrue(wait_until(lambda: self._up(port)))
            http = Connection(port)
            if _ == 0:
                self.assertEqual(http.post("/address", form(area="ni", place="Pirot"))[0], 303)
            else:
                self.assertIn("Pirot", http.request("GET", "/")[2])
            stop.set()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
