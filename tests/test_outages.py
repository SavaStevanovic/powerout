import unittest
from datetime import date, time

from powerout.outages import (
    AREAS,
    DAYS,
    Address,
    Fetcher,
    Numbers,
    Page,
    ParseError,
    Response,
    Row,
    covers,
    find_matches,
    match_row,
    parse_house,
    parse_numbers,
    parse_page,
)
from tests.helpers import fixture

TODAY = date(2026, 10, 30)


def make_page(title: str, rows: list[tuple[str, ...]], header=("Огранак", "Општина", "Време", "Улице")) -> bytes:
    """A page in the distributor's markup."""
    cells = lambda tag, values: "<tr>" + "".join(f"<{tag}>{v}</{tag}>" for v in values) + "</tr>"
    body = "".join(cells("td", row) for row in rows)
    return f"<HTML><BODY><TABLE><TR><TD><B>{title}</B></TD></TR></TABLE><TABLE>{cells('td', header)}{body}</TABLE></BODY></HTML>".encode()


def row(text: str, municipality="Медијана", branch="Ниш", when="09:00 - 14:00") -> Row:
    return parse_page(make_page("НИШ - Планирана искључења за датум: 30.10.2026.", [(branch, municipality, when, text)])).rows[0]


def address(place="Niš", street="", number="", area="ni") -> Address:
    return Address("a1", area, place, street, number)


class ParsePageTest(unittest.TestCase):
    def test_a_real_nis_page(self):
        page = parse_page(fixture("nis_2026-10-30.htm"))
        self.assertEqual(page.day, date(2026, 10, 30))
        self.assertEqual(len(page.rows), 21)
        first = page.rows[0]
        self.assertEqual((first.branch, first.municipality), ("Niš", "Doljevac"))
        self.assertEqual((first.start, first.end), (time(9, 0), time(10, 0)))
        self.assertEqual(first.text, "deo Pukovca")  # Latin script, trailing separator gone

    def test_a_day_without_outages_is_a_valid_empty_page(self):
        page = parse_page(fixture("nis_2026-10-31_empty.htm"))
        self.assertEqual((page.day, page.rows), (date(2026, 10, 31), ()))

    def test_belgrade_has_no_branch_column_and_an_iso_date(self):
        page = parse_page(fixture("beograd_2026-10-30.htm"))
        self.assertEqual(page.day, date(2026, 10, 30))
        self.assertEqual({r.branch for r in page.rows}, {""})
        self.assertEqual(page.rows[0].municipality, "Stari grad")
        self.assertTrue(page.rows[0].text.startswith("KRALJA PETRA: 9-11"))

    def test_quotes_around_the_text_are_dropped(self):
        page = parse_page(fixture("novisad_2026-10-31.htm"))
        self.assertTrue(page.rows[0].text.startswith("Sremska Kamenica: Fruškogorski put 109"))
        self.assertFalse(page.rows[0].text.startswith('"'))

    def test_a_page_that_is_not_the_table_is_an_error(self):
        for raw in [b"", b"<html><body>404</body></html>", make_page("x", [], header=("a", "b"))]:
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(ParseError):
                    parse_page(raw)

    def test_columns_are_found_by_their_headings_not_by_position(self):
        raw = make_page("НИШ 30.10.2026.", [("09:00 - 10:00", "Пирот", "Село Костур")], header=("Време", "Општина", "Улице"))
        [r] = parse_page(raw).rows
        self.assertEqual((r.municipality, r.text, r.branch), ("Pirot", "Selo Kostur", ""))

    def test_odd_times_are_kept_but_not_parsed(self):
        raw = make_page("НИШ 30.10.2026.", [("Ниш", "Пирот", "ceo dan", "Село"), ("Ниш", "Пирот", "22:00 - 24:00", "Село")])
        a, b = parse_page(raw).rows
        self.assertEqual((a.when, a.start, a.end), ("ceo dan", None, None))
        self.assertEqual((b.start, b.end), (time(22, 0), time(23, 59)))

    def test_rows_shorter_than_the_heading_are_skipped(self):
        raw = make_page("НИШ 30.10.2026.", [("Ниш", "Пирот"), ("Ниш", "Пирот", "09:00 - 10:00", "Село")])
        self.assertEqual(len(parse_page(raw).rows), 1)

    def test_entities_and_line_breaks_in_cells(self):
        raw = make_page("НИШ 30.10.2026.", [("Ниш", "Пирот", "09:00&nbsp;-&nbsp;10:00", "Село&nbsp;Костур<br>и&nbsp;Рсовци")])
        [r] = parse_page(raw).rows
        self.assertEqual((r.text, r.start), ("Selo Kostur i Rsovci", time(9, 0)))

    def test_a_missing_or_impossible_date_is_none(self):
        self.assertIsNone(parse_page(make_page("bez datuma", [])).day)
        self.assertIsNone(parse_page(make_page("НИШ 31.02.2026.", [])).day)


class NumbersTest(unittest.TestCase):
    def test_a_belgrade_list(self):
        listed = parse_numbers(": 4-24,1-13,19-21, D.ĐORĐEVIĆA-GOŠE: 8A-12,")
        self.assertEqual(listed.items, [Numbers(4, 24, ""), Numbers(1, 13, ""), Numbers(19, 21, "")])
        self.assertTrue(listed.reliable)

    def test_letters_and_no_number_markers(self):
        self.assertEqual(parse_numbers(": BB,BB,4-8,12,19G-19I, 11-11v").items, [Numbers(4, 8, ""), Numbers(12, None, ""), Numbers(19, 19, ""), Numbers(11, 11, "")])

    def test_a_house_in_brackets(self):
        listed = parse_numbers(" (91 b)")
        self.assertEqual(listed.items, [Numbers(91, None, "b")])
        self.assertTrue(listed.reliable)

    def test_ranges_with_spaces_and_en_dashes(self):
        listed = parse_numbers(" 109 – 153, 172 – 230; Староиришки put")
        self.assertEqual(listed.items, [Numbers(109, 153, ""), Numbers(172, 230, "")])

    def test_a_lone_number_in_prose_is_not_reliable(self):
        listed = parse_numbers(" 170 prema autoputu")
        self.assertEqual(listed.items, [Numbers(170, None, "")])
        self.assertFalse(listed.reliable)  # "from 170 on", not "only 170"

    def test_the_first_letter_of_the_next_word_is_not_a_suffix(self):
        self.assertEqual(parse_numbers(" 12 prema").items, [Numbers(12, None, "")])

    def test_no_numbers(self):
        for text in ["", " i deo ulice", ", Drugа ulica: 5"]:
            with self.subTest(text=text):
                self.assertEqual(parse_numbers(text).items, [])

    def test_where_the_list_sits(self):
        text = ": 9-11, X"
        listed = parse_numbers(text)
        self.assertEqual(text[slice(*listed.where)], "9-11")


class CoversTest(unittest.TestCase):
    def test_a_range_with_two_even_ends_is_the_even_side(self):
        numbers = [Numbers(2, 12, "")]
        self.assertTrue(all(covers(numbers, (n, "")) for n in (2, 4, 12)))
        self.assertFalse(any(covers(numbers, (n, "")) for n in (1, 3, 11, 14)))

    def test_a_range_with_two_odd_ends_is_the_odd_side(self):
        numbers = [Numbers(1, 5, "")]
        self.assertTrue(all(covers(numbers, (n, "")) for n in (1, 3, 5)))
        self.assertFalse(any(covers(numbers, (n, "")) for n in (2, 4, 6)))

    def test_a_range_that_mixes_parities_is_every_number(self):
        numbers = [Numbers(1, 4, "")]
        self.assertTrue(all(covers(numbers, (n, "")) for n in (1, 2, 3, 4)))
        self.assertFalse(covers(numbers, (5, "")))

    def test_any_of_several_items(self):
        numbers = [Numbers(2, 4, ""), Numbers(1, 5, ""), Numbers(8, None, "")]
        self.assertTrue(covers(numbers, (3, "")))
        self.assertTrue(covers(numbers, (8, "")))
        self.assertFalse(covers(numbers, (6, "")))

    def test_a_single_house_with_a_letter(self):
        numbers = [Numbers(91, None, "b")]
        self.assertTrue(covers(numbers, (91, "b")))
        self.assertTrue(covers(numbers, (91, "")))  # a person who left the letter out is still warned
        self.assertFalse(covers(numbers, (91, "v")))
        self.assertFalse(covers(numbers, (93, "")))


class ParseHouseTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_house("12"), (12, ""))
        self.assertEqual(parse_house(" 12a "), (12, "a"))
        self.assertEqual(parse_house("91 б"), (91, "b"))
        self.assertEqual(parse_house("5Ć"), (5, "c"))

    def test_nothing_usable(self):
        for text in ["", "bb", "bez broja", "-"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_house(text))


class MatchTest(unittest.TestCase):
    def match(self, text, addr, **row_kwargs):
        return match_row(addr, row(text, **row_kwargs), TODAY)

    def test_a_street_written_in_another_case_is_found(self):
        m = self.match("делови улица Требињске, Космајске, Бубањске долине и Војводе Путника", address(street="Trebinjska"))
        self.assertEqual((m.level, m.reason), ("likely", "street_open"))

    def test_the_words_that_matched_are_marked(self):
        r = row("делови улица Требињске, Космајске и Бубањске долине")
        m = match_row(address(street="Trebinjska"), r, TODAY)
        self.assertEqual([r.text[a:b] for a, b in m.highlights], ["Trebinjske"])

    def test_a_house_inside_the_listed_numbers(self):
        m = self.match("КРАЉА ПЕТРА: 9-11,", address("Stari grad", "Kralja Petra", "9"), municipality="Стари град", branch="")
        self.assertEqual((m.level, m.reason), ("likely", "street_number"))

    def test_a_house_outside_the_listed_numbers_is_a_near_miss(self):
        m = self.match("КРАЉА ПЕТРА: 9-11,", address("Stari grad", "Kralja Petra", "10"), municipality="Стари град", branch="")
        self.assertEqual((m.level, m.reason), ("near", "number_outside"))

    def test_without_a_house_number_the_person_is_asked_for_it(self):
        m = self.match("КРАЉА ПЕТРА: 9-11,", address("Stari grad", "Kralja Petra"), municipality="Стари град", branch="")
        self.assertEqual((m.level, m.reason), ("likely", "street_ask_number"))

    def test_the_numbers_are_marked_too(self):
        r = row("КРАЉА ПЕТРА: 9-11, ДРУГА: 2", municipality="Стари град", branch="")
        m = match_row(address("Stari grad", "Kralja Petra", "9"), r, TODAY)
        self.assertEqual([r.text[a:b] for a, b in m.highlights], ["KRALJA", "PETRA", "9-11"])

    def test_a_street_named_in_prose_without_numbers_may_be_only_part_of_it(self):
        m = self.match("делови улица Милана Благојевића и Франца Винтера", address(street="Milana Blagojevića", number="5"))
        self.assertEqual((m.level, m.reason), ("likely", "street_open"))

    def test_a_lone_number_in_prose_does_not_rule_a_house_out(self):
        m = self.match("Од ТЕНЕНА и од Вука Караџића 170 према аутопуту", address(street="Vuka Karadžića", number="172"))
        self.assertEqual((m.level, m.reason), ("likely", "street_open"))

    def test_the_best_occurrence_wins(self):
        text = "ЦАРА ДУШАНА: 2-4, ДРУГА: 1, ЦАРА ДУШАНА: 7-9"  # the even numbers 2, 4 and the odd ones 7, 9
        self.assertEqual(self.match(text, address(street="Cara Dušana", number="9")).reason, "street_number")
        self.assertEqual(self.match(text, address(street="Cara Dušana", number="4")).reason, "street_number")
        self.assertEqual(self.match(text, address(street="Cara Dušana", number="8")).reason, "number_outside")

    def test_the_same_street_in_another_place_is_not_a_match(self):
        r = row("Зајечар улице: Хајдук Вељкова, део Цара Душана", municipality="Зајечар", branch="Зајечар")
        self.assertIsNone(match_row(address("Niš", "Cara Dušana"), r, TODAY))
        self.assertEqual(match_row(address("Zaječar", "Cara Dušana"), r, TODAY).level, "likely")

    def test_a_village_without_a_street(self):
        m = self.match("део Пуковца", address("Pukovac"), municipality="Дољевац")
        self.assertEqual((m.level, m.reason), ("likely", "place_only"))

    def test_a_village_named_in_the_text_while_the_person_gave_a_street_is_only_possible(self):
        m = self.match("Ченеј: Од ТЕНЕНА према центру", address("Čenej", "Neka ulica"), municipality="Нови Сад", branch="Нови Сад")
        self.assertEqual((m.level, m.reason), ("possible", "place_in_text"))

    def test_a_structured_list_that_lacks_the_street_rules_the_village_out(self):
        m = self.match("Насеље РИПАЊ: ЕРЧАНСКА: 76А-88,", address("Ripanj", "Dragoslava Mišića", "5"), municipality="Вождовац", branch="")
        self.assertIsNone(m)

    def test_only_the_municipality_is_the_area_level(self):
        m = self.match("део улице Винтерове", address("Medijana", "Neka druga ulica"))
        self.assertEqual((m.level, m.reason), ("area", "area"))

    def test_a_municipality_that_is_not_the_persons_is_nothing(self):
        self.assertIsNone(self.match("део улице Винтерове", address("Pirot", "Neka druga ulica")))

    def test_the_city_of_a_page_without_branches_counts(self):
        m = self.match("КРАЉА ПЕТРА: 9-11,", address("Beograd", "Kralja Petra", "9"), municipality="Стари град", branch="")
        self.assertIsNone(m)  # no home given: "Beograd" is nowhere in the row
        r = row("КРАЉА ПЕТРА: 9-11,", municipality="Стари град", branch="")
        self.assertEqual(match_row(address("Beograd", "Kralja Petra", "9"), r, TODAY, home="Beograd").level, "likely")

    def test_cyrillic_and_latin_and_missing_diacritics_are_all_the_same(self):
        for street in ["Требињска", "Trebinjska", "TREBINJSKA", "ulica Trebinjska"]:
            with self.subTest(street=street):
                self.assertEqual(self.match("делови улица Требињске", address(street=street)).level, "likely")
        self.assertEqual(self.match("улица Ђорђа Јовановића", address(street="Djordja Jovanovica")).level, "likely")

    def test_an_abbreviated_saint(self):
        m = self.match("Булевара Св. Пантелејмона (91 б)", address(street="Bulevar Svetog Pantelejmona", number="91b"))
        self.assertEqual((m.level, m.reason), ("likely", "street_number"))

    def test_only_the_settlement_the_person_named_is_searched(self):
        # one Belgrade row lists many settlements; "Kosmajska" exists in two of them, with different numbers
        r = row(
            "Naselje MANIĆ: KOSMAJSKA: 63-65, Naselje SLATINA: KOSMAJSKA: 1-7,67",
            municipality="Сопот",
            branch="",
        )
        # house 5 is in Slatina's list (1-7) but not in Manić's (63-65); searching the whole row would warn both
        manic = match_row(address("Manić", "Kosmajska", "5"), r, TODAY)
        slatina = match_row(address("Slatina", "Kosmajska", "5"), r, TODAY)
        self.assertEqual((manic.level, manic.reason), ("near", "number_outside"))
        self.assertEqual((slatina.level, slatina.reason), ("likely", "street_number"))
        self.assertEqual(match_row(address("Manić", "Kosmajska", "65"), r, TODAY).reason, "street_number")

    def test_when_the_place_is_the_municipality_the_whole_row_is_searched(self):
        r = row("Naselje MANIĆ: KOSMAJSKA: 63-65, Naselje SLATINA: KOSMAJSKA: 1-7,67", municipality="Сопот", branch="")
        self.assertEqual(match_row(address("Sopot", "Kosmajska", "5"), r, TODAY).reason, "street_number")

    def test_the_key_identifies_the_announcement_and_the_address(self):
        r = row("делови улица Требињске")
        a = match_row(address(street="Trebinjska"), r, TODAY)
        self.assertEqual(a.key, match_row(address(street="Trebinjska"), r, TODAY).key)
        self.assertNotEqual(a.key, match_row(address(street="Trebinjska"), r, date(2026, 10, 31)).key)
        self.assertNotEqual(a.key, match_row(Address("other", "ni", "Niš", "Trebinjska"), r, TODAY).key)
        self.assertNotEqual(a.key, match_row(address(street="Trebinjska"), row("делови улица Требињске и још једне"), TODAY).key)


class RealPagesTest(unittest.TestCase):
    """Addresses whose outcome is known from reading the announcements."""

    def setUp(self):
        self.nis = [parse_page(fixture("nis_2026-10-30.htm")), parse_page(fixture("nis_2026-10-31_empty.htm"))]
        self.belgrade = [parse_page(fixture("beograd_2026-10-30.htm")), parse_page(fixture("beograd_2026-10-31.htm"))]

    def best(self, addr, pages):
        found = find_matches(addr, pages)
        return [(m.level, m.reason, m.row.municipality) for m in found if m.level != "area"]

    def test_trebinjska_street_in_nis(self):
        self.assertEqual(self.best(address(street="Trebinjska"), self.nis), [("likely", "street_open", "Palilula - Niš")])

    def test_bulevar_pantelejmona_91b_but_not_93(self):
        self.assertEqual(self.best(address(street="Bulevar Svetog Pantelejmona", number="91b"), self.nis), [("likely", "street_number", "Pantelej")])
        self.assertEqual(self.best(address(street="Bulevar Svetog Pantelejmona", number="93"), self.nis), [("near", "number_outside", "Pantelej")])

    def test_a_village_whose_name_is_declined_in_the_text(self):
        self.assertEqual(self.best(address("Pukovac"), self.nis), [("likely", "place_only", "Doljevac")])

    def test_a_street_that_exists_in_zajecar_does_not_alarm_nis(self):
        self.assertEqual(self.best(address("Niš", "Cara Dušana"), self.nis), [])
        self.assertEqual(self.best(address("Zaječar", "Cara Dušana"), self.nis), [("likely", "street_open", "Zaječar")])

    def test_a_street_nobody_announced_still_shows_the_municipality_rows(self):
        found = find_matches(address("Medijana", "Neka ulica"), self.nis)
        self.assertEqual([(m.level, m.row.municipality) for m in found], [("area", "Medijana")])

    def test_belgrade_even_and_odd_sides(self):
        addr = lambda n: Address("a", "bg", "Stari grad", "Kralja Petra", n)
        self.assertEqual(self.best(addr("9"), self.belgrade)[0], ("likely", "street_number", "Stari grad"))
        self.assertEqual(self.best(addr("10"), self.belgrade)[0], ("near", "number_outside", "Stari grad"))

    def test_belgrade_ripanj_section_of_a_big_row(self):
        addr = lambda n: Address("a", "bg", "Ripanj", "Erčanska", n)
        self.assertEqual(self.best(addr("78"), self.belgrade), [("likely", "street_number", "Voždovac")])
        self.assertEqual(self.best(addr("79"), self.belgrade), [("near", "number_outside", "Voždovac")])

    def test_a_settlement_that_carries_the_name_of_its_municipality_does_not_hide_the_rest_of_the_row(self):
        # the real Novi Beograd row ends with "Naselje NOVI BEOGRAD: ..." but "OBRENOVAČKA" stands before it
        for place in ("Novi Beograd", "Beograd"):
            with self.subTest(place=place):
                found = self.best(Address("a", "bg", place, "Obrenovačka", "4"), self.belgrade)
                self.assertEqual(found, [("likely", "street_number", "Novi Beograd")])
        self.assertEqual(self.best(Address("a", "bg", "Novi Beograd", "Obrenovačka", "5"), self.belgrade), [("near", "number_outside", "Novi Beograd")])
        # and the street of that last section is still found
        self.assertEqual(self.best(Address("a", "bg", "Novi Beograd", "Kazandžijska", "4"), self.belgrade), [("likely", "street_number", "Novi Beograd")])

    def test_matches_come_soonest_first(self):
        found = find_matches(address("Niš"), self.nis)
        self.assertEqual([m.row.start for m in found], sorted(m.row.start for m in found))

    def test_a_page_without_a_date_is_not_searched(self):
        self.assertEqual(find_matches(address("Niš"), [Page(None, tuple(self.nis[0].rows))]), [])


class FetcherTest(unittest.TestCase):
    def setUp(self):
        self.requests: list[tuple[str, dict]] = []
        self.replies: dict[str, Response | Exception] = {}
        self.sleeps: list[float] = []
        self.clock = 1000.0

    def opener(self, url, headers, timeout):
        self.requests.append((url, dict(headers)))
        reply = self.replies.get(url, Response(200, {}, make_page("НИШ - Планирана искључења за датум: 30.10.2026.", [("Ниш", "Пирот", "09:00 - 10:00", "Село")])))
        if isinstance(reply, Exception):
            raise reply
        return reply

    def fetcher(self):
        return Fetcher(self.opener, now=lambda: self.clock, sleep=self.sleeps.append)

    def url(self, day):
        return AREAS["ni"].url(day)

    def test_reads_the_four_days_of_the_area_and_waits_between_requests(self):
        data = self.fetcher().fetch("ni", TODAY)
        self.assertEqual([u for u, _ in self.requests], [self.url(d) for d in range(DAYS)])
        self.assertEqual(len(data.pages), DAYS)
        self.assertEqual((data.errors, data.checked_at), ((), 1000.0))
        self.assertEqual(len(self.sleeps), DAYS - 1)

    def test_identifies_itself(self):
        self.fetcher().fetch("ni", TODAY)
        self.assertTrue(self.requests[0][1]["User-Agent"].startswith("powerout/"))

    def test_asks_whether_a_page_changed_instead_of_downloading_it_again(self):
        fetcher = self.fetcher()
        body = make_page("НИШ - Планирана искључења за датум: 30.10.2026.", [("Ниш", "Пирот", "09:00 - 10:00", "Село")])
        self.replies[self.url(0)] = Response(200, {"etag": '"abc"', "last-modified": "Thu, 29 Oct 2026 23:30:04 GMT"}, body)
        fetcher.fetch("ni", TODAY)
        self.requests.clear()
        self.replies[self.url(0)] = Response(304, {}, b"")
        data = fetcher.fetch("ni", TODAY)
        sent = self.requests[0][1]
        self.assertEqual((sent["If-None-Match"], sent["If-Modified-Since"]), ('"abc"', "Thu, 29 Oct 2026 23:30:04 GMT"))
        self.assertEqual((data.errors, len(data.pages[0].rows)), ((), 1))

    def test_a_failing_page_is_reported_and_the_old_copy_is_kept(self):
        fetcher = self.fetcher()
        fetcher.fetch("ni", TODAY)
        self.replies[self.url(1)] = Response(500, {}, b"")
        data = fetcher.fetch("ni", TODAY)
        self.assertEqual(len(data.pages), DAYS)
        self.assertEqual(len(data.errors), 1)
        self.assertIn("HTTP 500", data.errors[0])
        self.assertIn("dan 1", data.errors[0])

    def test_a_failing_page_without_an_old_copy_is_just_missing(self):
        self.replies[self.url(2)] = Response(404, {}, b"")
        data = self.fetcher().fetch("ni", TODAY)
        self.assertEqual(len(data.pages), DAYS - 1)

    def test_network_errors_do_not_escape(self):
        self.replies[self.url(0)] = OSError("Name or service not known")
        data = self.fetcher().fetch("ni", TODAY)
        self.assertIn("Name or service not known", data.errors[0])
        self.assertEqual(len(data.pages), DAYS - 1)

    def test_a_page_that_changed_its_format_is_an_error_not_an_empty_day(self):
        self.replies[self.url(0)] = Response(200, {}, b"<html>drugi format</html>")
        data = self.fetcher().fetch("ni", TODAY)
        self.assertIn("Opština", data.errors[0])
        self.assertEqual(len(data.pages), DAYS - 1)

    def test_an_oversized_answer_is_refused(self):
        self.replies[self.url(0)] = Response(200, {}, b"x" * 2_000_001)
        self.assertIn("prevelik", self.fetcher().fetch("ni", TODAY).errors[0])

    def test_a_page_without_a_date_gets_the_day_it_was_asked_for(self):
        for d in range(DAYS):
            self.replies[self.url(d)] = Response(200, {}, make_page("bez datuma", []))
        data = self.fetcher().fetch("ni", TODAY)
        self.assertEqual([p.day for p in data.pages], [date(2026, 10, 30 + d) if d < 2 else date(2026, 11, d - 1) for d in range(DAYS)])


class AreasTest(unittest.TestCase):
    def test_urls_are_the_ones_the_distributor_publishes(self):
        base = "https://www.elektrodistribucija.rs"
        self.assertEqual(AREAS["ni"].url(0), f"{base}/planirana-iskljucenja-srbija/Nis_Dan_0_Iskljucenja.htm")
        self.assertEqual(AREAS["bg"].url(3), f"{base}/planirana-iskljucenja-beograd/Dan_3_Iskljucenja.htm")
        self.assertEqual(AREAS["ns"].url(1), f"{base}/planirana-iskljucenja-srbija/NoviSad_Dan_1_Iskljucenja.htm")
        self.assertEqual(AREAS["kv"].page_url, f"{base}/planirana-iskljucenja/planirana-kv")


if __name__ == "__main__":
    unittest.main()
