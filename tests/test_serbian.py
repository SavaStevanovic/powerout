import unittest

from powerout.serbian import find_phrase, fold, has_words, query_keys, same_word, to_latin, tokenize


class TransliterationTest(unittest.TestCase):
    def test_cyrillic_to_latin(self):
        self.assertEqual(to_latin("Булевар Немањића"), "Bulevar Nemanjića")
        self.assertEqual(to_latin("Ђорђе Џон Љубомир Њега"), "Đorđe Džon Ljubomir Njega")

    def test_digraphs_follow_the_case_of_the_word(self):
        self.assertEqual(to_latin("КРАЉА ПЕТРА"), "KRALJA PETRA")
        self.assertEqual(to_latin("Краља Петра"), "Kralja Petra")
        self.assertEqual(to_latin("ЊЕГОШЕВА"), "NJEGOŠEVA")
        self.assertEqual(to_latin("Љ"), "Lj")

    def test_other_characters_are_left_alone(self):
        self.assertEqual(to_latin("Ul. 27. marta, br. 5-9 (91 б)"), "Ul. 27. marta, br. 5-9 (91 b)")


class FoldTest(unittest.TestCase):
    def test_every_way_of_writing_a_name_gives_one_skeleton(self):
        for written in ["Ђорђе", "Đorđe", "Djordje", "ĐORĐE", "djordje"]:
            with self.subTest(written=written):
                self.assertEqual(fold(written), "djordje")

    def test_diacritics_are_dropped(self):
        self.assertEqual(fold("Čačak, Šabac, Žarkovo, Ćuprija"), "cacak, sabac, zarkovo, cuprija")
        self.assertEqual(fold("Немањића"), fold("Nemanjica"))

    def test_the_dzh_digraph_survives_either_alphabet(self):
        self.assertEqual(fold("Џон"), fold("Džon"))


class TokenizeTest(unittest.TestCase):
    def test_tokens_keep_their_positions(self):
        text = "Bulevar Nemanjića 12"
        tokens = tokenize(text)
        self.assertEqual([t.key for t in tokens], ["bulevar", "nemanjica", "12"])
        self.assertEqual([text[t.start : t.end] for t in tokens], ["Bulevar", "Nemanjića", "12"])

    def test_abbreviations_of_saint_are_one_word(self):
        for text in ["Sv. Save", "Svetog Save", "Sveti Sava", "Svetoga Save"]:
            with self.subTest(text=text):
                self.assertEqual(tokenize(text)[0].key, "sv")

    def test_hyphens_and_dots_split_words(self):
        self.assertEqual([t.key for t in tokenize("D.Đorđevića-Goše")], ["d", "djordjevica", "gose"])

    def test_a_letter_with_a_full_stop_is_an_initial(self):
        marks = {t.key: t.initial for t in tokenize("D.Đorđevića i J. Ilića 5. a")}
        self.assertTrue(marks["d"])
        self.assertTrue(marks["j"])
        self.assertFalse(marks["i"])  # the conjunction, no full stop
        self.assertFalse(marks["5"])
        self.assertFalse(marks["a"])

    def test_query_keys_drops_the_generic_words(self):
        self.assertEqual(query_keys("Ulica Trebinjska"), ["trebinjska"])
        self.assertEqual(query_keys("Булевар Немањића"), ["nemanjica"])

    def test_query_keys_keeps_a_name_that_is_only_generic_words(self):
        self.assertEqual(query_keys("Trg"), ["trg"])


class SameWordTest(unittest.TestCase):
    def test_case_endings_are_tolerated(self):
        pairs = [
            ("trebinjska", "trebinjske"),
            ("bulevar", "bulevara"),
            ("milana", "milanom"),
            ("sava", "save"),
            ("vinterova", "vinterove"),
            ("jugoslovenska", "jugoslovenskih"),
            ("nemanjica", "nemanjicu"),
            ("nis", "nisa"),
            ("car", "cara"),
        ]
        for a, b in pairs:
            with self.subTest(a=a, b=b):
                self.assertTrue(same_word(a, b))
                self.assertTrue(same_word(b, a))

    def test_the_fleeting_a_of_place_names_is_handled(self):
        # nominative Pukovac, but the announcement says "део Пуковца"
        for a, b in [("pukovac", "pukovca"), ("leskovac", "leskovcu"), ("kragujevac", "kragujevcem"), ("lazarevac", "lazarevca")]:
            with self.subTest(a=a, b=b):
                self.assertTrue(same_word(a, b))
                self.assertTrue(same_word(b, a))

    def test_different_words_are_not_confused(self):
        pairs = [
            ("dusana", "dusanova"),  # a street named after Dušan vs. one named Dušanova
            ("rada", "radoja"),
            ("nikola", "nikolic"),
            ("kralja", "kraljevica"),
            ("palilula", "medijana"),
            ("save", "savska"),
            ("pukovac", "pukovnik"),
        ]
        for a, b in pairs:
            with self.subTest(a=a, b=b):
                self.assertFalse(same_word(a, b))

    def test_words_too_short_to_share_a_stem_must_be_identical(self):
        self.assertTrue(same_word("za", "za"))
        self.assertFalse(same_word("za", "zi"))
        self.assertFalse(same_word("ive", "ivan"))

    def test_numbers_must_be_identical(self):
        self.assertTrue(same_word("27", "27"))
        self.assertFalse(same_word("27", "28"))
        self.assertFalse(same_word("91b", "91"))


class FindPhraseTest(unittest.TestCase):
    def find(self, query, text, **kwargs):
        tokens = tokenize(to_latin(text))
        return find_phrase(query_keys(query), tokens, **kwargs), tokens

    def test_finds_a_street_written_in_another_case(self):
        hits, tokens = self.find("Trebinjska", "делови улица Требињске, Космајске и Бубањске долине")
        self.assertEqual(len(hits), 1)
        self.assertEqual(tokens[hits[0][0]].text, "Trebinjske")

    def test_all_words_must_follow_each_other(self):
        text = "део Цара Душана и доњи део Ивана Милутиновића"
        self.assertEqual(len(self.find("Cara Dušana", text)[0]), 1)
        self.assertEqual(self.find("Dušana Cara", text)[0], [])  # wrong order
        self.assertEqual(self.find("Cara Milutinovića", text)[0], [])  # too far apart

    def test_a_few_words_may_sit_between(self):
        hits, _ = self.find("Bulevar Svetog Pantelejmona", "Булевара Св. Пантелејмона (91 б)")
        self.assertEqual(len(hits), 1)
        hits, _ = self.find("Pantelejmona", "Булевара Св. Пантелејмона")
        self.assertEqual(len(hits), 1)

    def test_the_saint_may_be_abbreviated_on_either_side(self):
        self.assertEqual(len(self.find("Sv. Save", "улица Светог Саве")[0]), 1)
        self.assertEqual(len(self.find("Svetog Save", "Св. Саве 12")[0]), 1)

    def test_numbers_in_a_name_are_matched_exactly(self):
        self.assertEqual(len(self.find("27. marta", "улица Јосипа Краша, 27. Марта, Коче Рацина")[0]), 1)
        self.assertEqual(self.find("28. marta", "улица Јосипа Краша, 27. Марта, Коче Рацина")[0], [])

    def test_an_empty_query_finds_nothing(self):
        self.assertEqual(find_phrase([], tokenize("нешто")), [])

    def test_an_initial_in_the_text_stands_for_the_first_name(self):
        text = "Д.ЂОРЂЕВИЋА-ГОШЕ: 8А-12"
        self.assertEqual(len(self.find("Dragutina Đorđevića-Goše", text)[0]), 1)
        self.assertEqual(len(self.find("Dušana Đorđevića-Goše", text)[0]), 1)
        self.assertEqual(self.find("Marka Đorđevića-Goše", text)[0], [])  # another letter

    def test_a_word_that_is_not_an_initial_does_not_match_by_its_first_letter(self):
        # "и" is the conjunction "and"; only a letter followed by a full stop is an initial
        self.assertEqual(self.find("Ilije Garašanina", "ulica Ive и Garašanina")[0], [])
        self.assertEqual(self.find("Ilije Garašanina", "улица И.Гарашанина")[0], [[1, 2]])  # the initial and the surname

    def test_every_occurrence_is_reported(self):
        hits, _ = self.find("Nušićeva", "Нушићева и још једном Нушићева")
        self.assertEqual(len(hits), 2)


class HasWordsTest(unittest.TestCase):
    def test_words_in_any_order(self):
        tokens = tokenize(to_latin("Палилула - Ниш"))
        self.assertTrue(has_words(["nis"], tokens))
        self.assertTrue(has_words(["nis", "palilula"], tokens))
        self.assertFalse(has_words(["nis", "medijana"], tokens))
        self.assertFalse(has_words([], tokens))


if __name__ == "__main__":
    unittest.main()
