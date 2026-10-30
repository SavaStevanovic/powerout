import re
import unittest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from powerout import pages
from powerout.outages import Address, AreaData, Match, Page, Row, find_matches
from powerout.pages import AddressResult, View, judge, marked, render, timetable

TZ = ZoneInfo("Europe/Belgrade")
NOW = datetime(2026, 10, 30, 11, 0, tzinfo=TZ)
TODAY = NOW.date()
CHECKED = datetime(2026, 10, 30, 10, 45, tzinfo=TZ).timestamp()


def row(text="delovi ulica Trebinjske", start=(9, 0), end=(14, 0), municipality="Palilula - Niš", branch="Niš") -> Row:
    when = f"{start[0]:02}:{start[1]:02} - {end[0]:02}:{end[1]:02}"
    return Row(branch, municipality, when, time(*start), time(*end), text)


def page(day_offset=0, *rows: Row) -> Page:
    return Page(TODAY + timedelta(days=day_offset), tuple(rows))


def result(*day_rows: tuple[int, Row], street="Trebinjska", place="Niš", number="", errors=(), empty_days=(0, 1, 2, 3)) -> AddressResult:
    """An address whose area has the given announcements; `empty_days` are the days that were read."""
    by_day: dict[int, list[Row]] = {d: [] for d in empty_days}
    for offset, r in day_rows:
        by_day.setdefault(offset, []).append(r)
    data = AreaData(tuple(page(d, *rs) for d, rs in sorted(by_day.items())), CHECKED, tuple(errors))
    address = Address("a1", "ni", place, street, number, "Stan")
    return AddressResult(address, tuple(find_matches(address, list(data.pages))), data)


class JudgeTest(unittest.TestCase):
    def test_nothing_is_known_yet(self):
        v = judge(AddressResult(Address("a", "ni", "Niš"), (), None), NOW)
        self.assertEqual((v.tone, v.text), ("unknown", "Čitam najave distributera…"))

    def test_the_site_cannot_be_read(self):
        data = AreaData((), CHECKED, ("Niš, dan 0: HTTP 500",))
        v = judge(AddressResult(Address("a", "ni", "Niš"), (), data), NOW)
        self.assertEqual(v.tone, "unknown")
        self.assertIn("HTTP 500", v.note)

    def test_only_old_pages_are_not_good_news(self):
        old = AreaData((Page(date(2026, 10, 27), ()),), CHECKED, ("x",))
        v = judge(AddressResult(Address("a", "ni", "Niš"), (), old), NOW)
        self.assertEqual(v.tone, "unknown")
        self.assertIn("stari", v.text)

    def test_a_cut_later_today(self):
        v = judge(result((0, row(start=(15, 0), end=(17, 0)))), NOW)
        self.assertEqual((v.tone, v.text), ("alarm", "Danas od 15:00 do 17:00 verovatno nema struje."))

    def test_a_cut_tomorrow(self):
        v = judge(result((1, row())), NOW)
        self.assertEqual(v.text, "Sutra od 09:00 do 14:00 verovatno nema struje.")

    def test_a_cut_further_ahead_is_named_by_its_weekday(self):
        self.assertEqual(judge(result((3, row())), NOW).text, "Ponedeljak od 09:00 do 14:00 verovatno nema struje.")

    def test_a_cut_in_progress(self):
        v = judge(result((0, row(start=(9, 0), end=(14, 0)))), NOW)
        self.assertEqual((v.tone, v.text), ("alarm", "Struje trenutno verovatno nema, do 14:00."))

    def test_a_cut_that_just_ended_is_said_so(self):
        later = datetime(2026, 10, 30, 14, 5, tzinfo=TZ)
        v = judge(result((0, row(end=(14, 0)))), later)
        self.assertEqual(v.tone, "clear")
        self.assertEqual(v.text, "Isključenje je završeno u 14:00. Nema novih najava za tvoju adresu u narednih 4 dana.")

    def test_more_than_one_is_counted(self):
        v = judge(result((0, row()), (1, row())), NOW)
        self.assertTrue(v.text.endswith("Ima još najava za tvoju adresu: 1."))

    def test_the_place_only_is_a_maybe(self):
        v = judge(result((1, row("Čenej: Od TENENA prema centru", municipality="Novi Sad", branch="Novi Sad")), street="Neka ulica", place="Čenej"), NOW)
        self.assertEqual((v.tone, v.text), ("maybe", "Sutra od 09:00 do 14:00 možda nema struje. Pročitaj najavu ispod."))

    def test_a_house_outside_the_numbers_is_a_near_miss(self):
        v = judge(result((1, row("KRALJA PETRA: 9-11,")), street="Kralja Petra", number="10"), NOW)
        self.assertEqual(v.tone, "maybe")
        self.assertIn("tvoj broj nije u navedenim opsezima", v.text)

    def test_nothing_for_the_address(self):
        v = judge(result(), NOW)
        self.assertEqual((v.tone, v.text), ("clear", "Nema najave za tvoju adresu u narednih 4 dana."))

    def test_other_cuts_in_the_area_are_mentioned_but_do_not_change_the_answer(self):
        v = judge(result((0, row("deo Pukovca", municipality="Doljevac"))), NOW)
        self.assertEqual(v.tone, "clear")
        self.assertIn("drugih najava (1)", v.note)

    def test_days_that_could_not_be_read_are_admitted(self):
        v = judge(result(empty_days=(0, 1)), NOW)
        self.assertEqual(v.text, "Nema najave za tvoju adresu (pročitano 2 od 4 dana).")

    def test_a_failed_check_is_mentioned_next_to_the_old_answer(self):
        v = judge(result((1, row()), errors=("Niš, dan 2: HTTP 500",)), NOW)
        self.assertEqual(v.tone, "alarm")
        self.assertIn("Poslednja provera nije uspela: Niš, dan 2: HTTP 500", v.note)


class MarkedTest(unittest.TestCase):
    def test_wraps_the_spans_and_escapes_the_rest(self):
        self.assertEqual(marked("a <b> & c", ((2, 5),)), "a <mark>&lt;b&gt;</mark> &amp; c")

    def test_overlapping_and_unsorted_spans_do_not_break_the_text(self):
        self.assertEqual(marked("abcdef", ((3, 5), (1, 4))), "a<mark>bcd</mark><mark>e</mark>f")

    def test_no_spans(self):
        self.assertEqual(marked("<x>", ()), "&lt;x&gt;")


class TimetableTest(unittest.TestCase):
    def html(self, res, now=NOW):
        return timetable(res, now)

    def test_one_row_per_day_with_names_and_dates(self):
        out = self.html(result())
        for expected in ("danas", "sutra", "prekosutra", "ponedeljak", "pet 30.10.", "sub 31.10.", "ned 1.11.", "pon 2.11."):
            self.assertIn(expected, out)
        self.assertEqual(out.count('<li class="day">'), 4)

    def test_a_cut_is_drawn_where_its_hours_are(self):
        out = self.html(result((0, row(start=(9, 0), end=(14, 0)))))
        self.assertIn("left:37.500%;width:20.833%", out)
        self.assertIn('class="seg likely"', out)
        self.assertIn(">09–14<", out)

    def test_minutes_are_shown_when_they_matter(self):
        out = self.html(result((0, row(start=(8, 30), end=(14, 30)))))
        self.assertIn(">08:30–14:30<", out)

    def test_a_narrow_bar_gets_its_hours_beside_it(self):
        out = self.html(result((0, row(start=(13, 0), end=(14, 0)))))
        self.assertIn('class="seg likely"', out)
        self.assertIn('class="cap', out)
        self.assertIn(">13–14<", out)

    def test_a_cut_that_is_over_is_faded_and_the_needle_marks_now(self):
        out = self.html(result((0, row(end=(10, 0)))), now=datetime(2026, 10, 30, 11, 0, tzinfo=TZ))
        self.assertIn("past", out)
        self.assertEqual(out.count('class="needle"'), 1)
        self.assertIn("left:45.833%", out)  # 11:00 of 24:00

    def test_overlapping_cuts_get_their_own_lane(self):
        out = self.html(result((0, row(start=(9, 0), end=(14, 0))), (0, row("Trebinjska 5", start=(10, 0), end=(12, 0)))))
        self.assertIn("--lane:0", out)
        self.assertIn("--lane:1", out)
        self.assertIn("--lanes:2", out)

    def test_hours_that_cannot_be_read_mean_the_whole_day(self):
        unreadable = Row("Niš", "Palilula - Niš", "ceo dan", None, None, "delovi ulica Trebinjske")
        out = self.html(result((0, unreadable)))
        self.assertIn("left:0.000%;width:100.000%", out)
        self.assertIn("ceo dan", out)

    def test_an_empty_day_says_so_for_everyone_including_screen_readers(self):
        out = self.html(result())
        self.assertEqual(out.count("nema najave"), 8)  # drawn and spoken, for four days
        self.assertIn('aria-hidden="true"', out)

    def test_the_spoken_version_names_the_hours_and_the_certainty(self):
        out = self.html(result((1, row())))
        self.assertIn("sutra: 09:00 do 14:00, verovatno te pogađa", out)

    def test_nearby_announcements_of_the_area_are_not_drawn(self):
        out = self.html(result((0, row("deo Pukovca", municipality="Doljevac"))))
        self.assertNotIn('class="seg', out)


def view(*results: AddressResult, **kwargs) -> View:
    return View(now=NOW, results=tuple(results), channels=kwargs.pop("channels", ""), interval=30, **kwargs)


class RenderTest(unittest.TestCase):
    def test_the_first_visit_asks_for_an_address(self):
        out = render(view())
        self.assertIn("Gde stanuješ?", out)
        self.assertIn('action="/address"', out)
        self.assertNotIn("Dodaj još jednu adresu", out)

    def test_an_address_shows_its_verdict_the_timetable_and_the_announcement(self):
        out = render(view(result((1, row()))))
        self.assertIn("Sutra od 09:00 do 14:00 verovatno nema struje.", out)
        self.assertIn('class="tt"', out)
        self.assertIn("<mark>Trebinjske</mark>", out)
        self.assertIn("Pominje se tvoja ulica", out)
        self.assertIn("Dodaj još jednu adresu", out)
        self.assertIn("Sve najave u oblasti Niš (1)", out)

    def test_nothing_the_person_or_the_distributor_wrote_can_inject_markup(self):
        evil = '<script>alert(1)</script>"\''
        r = row(text=f"delovi ulica Trebinjske {evil}", municipality=evil)
        address = Address("a1", "ni", evil, "Trebinjska", "", evil)
        data = AreaData((page(1, r),), CHECKED, (evil,))
        res = AddressResult(address, tuple(find_matches(Address("a1", "ni", "Niš", "Trebinjska"), list(data.pages))), data)
        out = render(view(res, flash=evil, error=evil, form={"place": evil, "street": evil, "number": evil, "label": evil}, suggestions=(evil,)))
        self.assertNotIn("<script", out)
        self.assertNotIn("alert(1)</", out)
        self.assertIn("&lt;script&gt;", out)

    def test_it_loads_nothing_from_elsewhere(self):
        out = render(view(result((1, row()))))
        self.assertNotRegex(out, r"<script|<link[^>]+stylesheet|@import|src=\"https?:")
        links = set(re.findall(r'href="(https?://[^"]+)"', out))
        self.assertEqual({u.split("/")[2] for u in links}, {"www.elektrodistribucija.rs"})

    def test_no_day_is_declared_free_while_nothing_is_known(self):
        waiting = AddressResult(Address("a", "ni", "Niš"), (), None)
        unreadable = AddressResult(Address("a", "ni", "Niš"), (), AreaData((), CHECKED, ("Niš, dan 0: HTTP 500",)))
        stale = AddressResult(Address("a", "ni", "Niš"), (), AreaData((Page(date(2026, 10, 27), ()),), CHECKED, ("x",)))
        for res in (waiting, unreadable, stale):
            with self.subTest(data=res.data):
                out = render(view(res))
                self.assertNotIn('class="tt"', out)
                self.assertNotIn("nema najave", out)
        self.assertIn('class="tt"', render(view(result())))  # once the days are read, they are drawn

    def test_it_waits_quickly_for_the_first_data_and_then_calmly(self):
        waiting = render(view(AddressResult(Address("a", "ni", "Niš"), (), None)))
        self.assertIn('content="4"', waiting)
        self.assertIn('content="900"', render(view(result())))
        self.assertIn('content="4"', render(view(result(), busy=True)))

    def test_a_refused_form_keeps_what_was_typed_and_says_why(self):
        out = render(view(error="Mesto: ovo polje je obavezno.", form={"place": "Pirot", "street": "Nišava", "area": "ni"}))
        self.assertIn('role="alert"', out)
        self.assertIn("Mesto: ovo polje je obavezno.", out)
        self.assertIn('value="Pirot"', out)
        self.assertIn('value="Nišava"', out)

    def test_the_form_offers_the_places_the_distributor_mentioned(self):
        out = render(view(suggestions=("Doljevac", "Niš")))
        self.assertIn('<option value="Doljevac"></option>', out)

    def test_every_area_can_be_chosen_and_the_last_choice_is_kept(self):
        out = render(view(form={"area": "kv"}))
        for name in ("Beograd", "Novi Sad", "Kraljevo", "Kragujevac", "Niš"):
            self.assertIn(f">{name}</option>", out)
        self.assertRegex(out, r'<option value="kv" selected>')

    def test_notifications_without_a_channel_are_explained_not_offered(self):
        out = render(view(result()))
        self.assertIn("Obaveštenja nisu podešena", out)
        self.assertNotIn("Pošalji probnu poruku", out)
        self.assertNotIn("/notify", out)

    def test_notifications_with_a_channel_can_be_switched_and_tested(self):
        out = render(view(result(), channels="Telegram i e-mail"))
        self.assertIn("Obaveštenja idu na: Telegram i e-mail.", out)
        self.assertIn("Pošalji probnu poruku", out)
        self.assertIn("/address/a1/notify", out)
        self.assertIn("Obaveštenja za ovu adresu: uključena.", out)
        self.assertIn("Javi mi na Telegram i e-mail čim se najavi isključenje", out)

    def test_a_flash_message_is_announced_politely(self):
        self.assertIn('role="status">Sačuvano.', render(view(flash="Sačuvano.")))

    def test_the_page_says_what_it_cannot_do(self):
        out = render(view(result()))
        self.assertIn("Neplanirane nestanke struje distributer ne objavljuje", out)
        self.assertIn("zvanični sajt ostaje merodavan", out)

    def test_the_title_names_the_area_only_when_the_place_does_not(self):
        self.assertIn("Trebinjska, Niš<", render(view(result())))
        pirot = result(place="Pirot")
        self.assertIn("Trebinjska, Pirot, oblast Niš", render(view(pirot)))

    def test_a_language_and_a_title_are_declared(self):
        out = render(view())
        self.assertIn('<html lang="sr-Latn">', out)
        self.assertIn("<title>Struja: planirana isključenja</title>", out)
        self.assertEqual(out.count("<h1"), 1)

    def test_a_finished_announcement_is_marked(self):
        late = datetime(2026, 10, 30, 15, 0, tzinfo=TZ)
        out = render(pages.View(now=late, results=(result((0, row())),), channels="", interval=30))
        self.assertIn("završeno</span>", out)

    def test_long_announcements_of_other_places_are_clipped_in_the_full_list(self):
        long_row = row("x " * 1000, municipality="Doljevac")
        out = render(view(result((0, long_row))))
        self.assertIn("…", out)
        self.assertLess(len(out), 60_000)

    def test_the_full_list_marks_what_matched(self):
        out = render(view(result((0, row()), (0, row("deo Pukovca", municipality="Doljevac")))))
        self.assertIn("Sve najave u oblasti Niš (2)", out)
        self.assertIn("Verovatno te pogađa", out)
        self.assertIn("U tvojoj oblasti", out)


if __name__ == "__main__":
    unittest.main()
