import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from powerout.messages import Formatter, format_duration
from powerout.notify import Message
from tests.helpers import Clock

BELGRADE = ZoneInfo("Europe/Belgrade")


def ts(day, hour, minute, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=BELGRADE).timestamp()


class FormatDurationTest(unittest.TestCase):
    def test_shows_the_two_most_significant_units(self):
        cases = {
            0: "0 s",
            28: "28 s",
            60: "1 min",
            3328: "55 min 28 s",
            3601: "1 h",
            7500: "2 h 5 min",
            90000: "1 d 1 h",
        }
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds), expected)

    def test_negative_durations_do_not_go_negative(self):
        self.assertEqual(format_duration(-5), "0 s")


class FormatterTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(ts(30, 15, 0))
        self.fmt = Formatter(BELGRADE, self.clock)

    def test_time_omits_the_date_for_today(self):
        self.assertEqual(self.fmt.time(ts(30, 14, 3, 12)), "14:03:12")

    def test_time_includes_the_date_for_other_days(self):
        self.assertEqual(self.fmt.time(ts(29, 23, 59, 1)), "29.10. 23:59:01")

    def test_power_lost_names_the_location_and_the_time(self):
        message = self.fmt.power_lost("Stan", ts(30, 14, 3, 12))
        self.assertEqual(message.title, "⚡ Nestala struja: Stan")
        self.assertIn("14:03:12", message.body)

    def test_power_restored_reports_how_long_it_lasted(self):
        message = self.fmt.power_restored("Stan", ts(30, 14, 58, 40), 3328)
        self.assertIn("55 min 28 s", message.body)

    def test_restart_after_outage_says_how_long_the_device_was_off(self):
        message = self.fmt.restart_after_outage("Stan", ts(30, 11, 0), ts(30, 14, 30))
        self.assertEqual(message.title, "🔌 Uređaj je ponovo pokrenut: Stan")
        self.assertIn("3 h 30 min", message.body)

    def test_signal_lost_blames_the_power_only_when_the_device_was_on_battery(self):
        on_battery = self.fmt.signal_lost("Stan", ts(30, 14, 15), "battery", ts(30, 14, 20))
        on_mains = self.fmt.signal_lost("Stan", ts(30, 14, 15), "mains", ts(30, 14, 20))
        self.assertIn("verovatno nestala struja", on_battery.body)
        self.assertNotIn("verovatno nestala struja", on_mains.body)
        self.assertIn("pre 5 min", on_mains.body)

    def test_delayed_appends_a_note_to_the_body(self):
        original = Message("naslov", "telo")
        delayed = self.fmt.delayed(original, 420)
        self.assertEqual(delayed.title, "naslov")
        self.assertTrue(delayed.body.startswith("telo\n\n"))
        self.assertIn("7 min", delayed.body)

    def test_message_text_joins_title_and_body(self):
        self.assertEqual(Message("a", "b").text, "a\n\nb")
        self.assertEqual(Message("a").text, "a")


if __name__ == "__main__":
    unittest.main()
