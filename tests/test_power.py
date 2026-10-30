import stat
import tempfile
import unittest
from pathlib import Path

from powerout.config import ConfigError, Power
from powerout.power import (
    CommandSource,
    NutSource,
    PowerTracker,
    SysfsSource,
    detect_source,
)


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.tracker = PowerTracker(debounce=10)

    def feed(self, reading, at):
        """Monotonic time is `at`; wall-clock time is offset so the two cannot be confused."""
        return self.tracker.observe(reading, mono=at, wall=1000.0 + at)

    def test_steady_power_is_silent(self):
        for at in range(0, 100, 2):
            self.assertEqual(self.feed(True, at), [])

    def test_loss_is_confirmed_after_the_debounce_and_carries_the_first_seen_time(self):
        self.assertEqual(self.feed(False, 0), [])
        self.assertEqual(self.feed(False, 9), [])
        [event] = self.feed(False, 10)
        self.assertEqual((event.kind, event.at), ("lost", 1000.0))

    def test_a_short_blip_is_ignored(self):
        self.feed(False, 0)
        self.assertEqual(self.feed(True, 5), [])
        self.assertEqual(self.feed(True, 30), [])
        self.assertTrue(self.tracker.mains)

    def test_a_blip_restarts_the_debounce(self):
        self.feed(False, 0)
        self.feed(True, 5)
        self.feed(False, 6)
        self.assertEqual(self.feed(False, 15), [])
        [event] = self.feed(False, 16)
        self.assertEqual(event.at, 1006.0)

    def test_restore_reports_the_length_of_the_outage(self):
        self.feed(False, 0)
        self.feed(False, 10)
        self.feed(True, 100)
        [event] = self.feed(True, 110)
        self.assertEqual((event.kind, event.at, event.duration), ("restored", 1100.0, 100.0))

    def test_the_outage_length_counts_time_the_machine_slept(self):
        # a laptop that suspends during a long outage: the monotonic clock stood still, the wall clock did not
        self.tracker.observe(False, mono=0, wall=1000.0)
        self.tracker.observe(False, mono=10, wall=1010.0)
        self.tracker.observe(True, mono=100, wall=5000.0)
        [event] = self.tracker.observe(True, mono=110, wall=5010.0)
        self.assertEqual(event.duration, 4000.0)

    def test_a_wall_clock_that_jumped_back_never_gives_a_negative_length(self):
        self.tracker.observe(False, mono=0, wall=5000.0)
        self.tracker.observe(False, mono=10, wall=5010.0)
        self.tracker.observe(True, mono=100, wall=1000.0)
        [event] = self.tracker.observe(True, mono=110, wall=1010.0)
        self.assertEqual(event.duration, 0.0)

    def test_only_one_event_per_change(self):
        self.feed(False, 0)
        self.assertEqual(len(self.feed(False, 10)), 1)
        self.assertEqual(self.feed(False, 20), [])

    def test_unknown_readings_change_nothing(self):
        self.feed(False, 0)
        self.assertEqual(self.feed(None, 5), [])
        [event] = self.feed(False, 10)
        self.assertEqual(event.at, 1000.0)

    def test_starting_on_battery_is_reported_as_a_loss(self):
        self.assertEqual(self.feed(False, 0), [])
        [event] = self.feed(False, 10)
        self.assertEqual(event.kind, "lost")

    def test_zero_debounce_reports_immediately(self):
        tracker = PowerTracker(debounce=0)
        [event] = tracker.observe(False, mono=5, wall=50)
        self.assertEqual((event.kind, event.at), ("lost", 50))


def make_tree(root: Path, supplies: dict[str, dict[str, str]]) -> Path:
    for name, attrs in supplies.items():
        directory = root / name
        directory.mkdir(parents=True)
        for key, value in attrs.items():
            (directory / key).write_text(value + "\n")
    return root


class SysfsSourceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def source(self, supplies, supply=""):
        return SysfsSource(make_tree(self.root, supplies), supply)

    def test_laptop_on_adapter(self):
        source = self.source({"AC": {"type": "Mains", "online": "1"}, "BAT0": {"type": "Battery", "status": "Charging"}})
        self.assertTrue(source.usable())
        self.assertIs(source.read(), True)

    def test_laptop_on_battery(self):
        source = self.source(
            {"AC": {"type": "Mains", "online": "0"}, "BAT0": {"type": "Battery", "status": "Discharging"}}
        )
        self.assertIs(source.read(), False)

    def test_usb_c_charger_counts_as_external_power(self):
        source = self.source(
            {"ucsi-source-psy-USBC000:001": {"type": "USB", "online": "1"}, "BAT1": {"type": "Battery"}}
        )
        self.assertTrue(source.usable())
        self.assertIs(source.read(), True)

    def test_any_online_supply_is_enough(self):
        source = self.source(
            {
                "AC": {"type": "Mains", "online": "0"},
                "ucsi": {"type": "USB", "online": "1"},
                "BAT0": {"type": "Battery"},
            }
        )
        self.assertIs(source.read(), True)

    def test_desktop_without_supplies_is_not_usable(self):
        self.assertFalse(self.source({}).usable())

    def test_desktop_with_an_always_offline_usb_port_and_a_wireless_mouse_is_not_usable(self):
        # Without a system battery "offline" cannot be an outage, and a mouse battery is not one.
        source = self.source(
            {
                "ucsi": {"type": "USB", "online": "0"},
                "hidpp_battery_0": {"type": "Battery", "scope": "Device", "status": "Discharging"},
            }
        )
        self.assertFalse(source.usable())

    def test_missing_directory_is_not_usable(self):
        self.assertFalse(SysfsSource(self.root / "nope").usable())

    def test_unreadable_state_is_unknown(self):
        source = self.source({"AC": {"type": "Mains"}, "BAT0": {"type": "Battery"}})
        self.assertIsNone(source.read())

    def test_a_pinned_supply_is_read_alone(self):
        source = self.source(
            {"ADP1": {"type": "Mains", "online": "0"}, "ucsi": {"type": "USB", "online": "1"}}, supply="ADP1"
        )
        self.assertTrue(source.usable())
        self.assertIs(source.read(), False)

    def test_a_pinned_supply_that_does_not_exist_is_not_usable(self):
        self.assertFalse(self.source({"AC": {"type": "Mains", "online": "1"}}, supply="ADP1").usable())


class ScriptTestCase(unittest.TestCase):
    def script(self, body: str) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "script"
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)


class NutSourceTest(ScriptTestCase):
    def read(self, body):
        return NutSource("ups@localhost", binary=self.script(body)).read()

    def test_online(self):
        self.assertIs(self.read("echo OL"), True)
        self.assertIs(self.read("echo OL CHRG"), True)

    def test_on_battery(self):
        self.assertIs(self.read("echo OB DISCHRG"), False)
        self.assertIs(self.read("echo OB LB"), False)

    def test_other_states_are_unknown(self):
        self.assertIsNone(self.read("echo OFF"))
        self.assertIsNone(self.read("echo"))

    def test_a_failing_upsc_is_unknown(self):
        self.assertIsNone(self.read("echo 'Error: Data stale' >&2; echo OB; exit 1"))

    def test_asks_for_the_right_variable(self):
        self.assertIs(self.read('[ "$1 $2" = "ups@localhost ups.status" ] && echo OL'), True)

    def test_missing_binary_is_unknown(self):
        self.assertIsNone(NutSource("ups", binary="/nonexistent/upsc").read())


class CommandSourceTest(ScriptTestCase):
    def test_exit_codes(self):
        self.assertIs(CommandSource(self.script("exit 0")).read(), True)
        self.assertIs(CommandSource(self.script("exit 1")).read(), False)
        self.assertIsNone(CommandSource(self.script("exit 2")).read())

    def test_missing_command_is_unknown(self):
        self.assertIsNone(CommandSource("/nonexistent/check").read())

    def test_timeout_is_unknown(self):
        self.assertIsNone(CommandSource(self.script("sleep 5"), timeout=0.2).read())

    def test_arguments_are_split_like_a_shell_would(self):
        self.assertIs(CommandSource(f"{self.script('[ \"$1\" = \"a b\" ]')} 'a b'").read(), True)


class DetectSourceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_none_means_none(self):
        self.assertIsNone(detect_source(Power(source="none"), self.root))

    def test_auto_prefers_a_configured_ups_then_a_command(self):
        self.assertIsInstance(detect_source(Power(ups="ups@localhost", command="true"), self.root), NutSource)
        self.assertIsInstance(detect_source(Power(command="true"), self.root), CommandSource)

    def test_auto_falls_back_to_sysfs_on_a_laptop(self):
        make_tree(self.root, {"AC": {"type": "Mains", "online": "1"}, "BAT0": {"type": "Battery"}})
        self.assertIsInstance(detect_source(Power(), self.root), SysfsSource)

    def test_auto_on_a_desktop_finds_nothing(self):
        self.assertIsNone(detect_source(Power(), self.root))

    def test_explicit_sysfs_on_a_desktop_is_an_error(self):
        with self.assertRaises(ConfigError):
            detect_source(Power(source="sysfs"), self.root)


if __name__ == "__main__":
    unittest.main()
