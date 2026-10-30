import tempfile
import threading
import unittest
from datetime import timezone
from pathlib import Path

from powerout.agent import MIN_GAP, STATE_INTERVAL, Agent, HeartbeatSender
from powerout.config import Heartbeat
from powerout.messages import Formatter
from powerout.power import PowerTracker
from powerout.store import JsonStore
from tests.helpers import Clock, FakeDispatcher, StubServer, wait_until


class FakeSource:
    description = "fake"

    def __init__(self, reading=True):
        self.reading = reading

    def read(self):
        return self.reading


class FakeHeartbeat:
    def __init__(self):
        self.pokes = 0

    def poke(self):
        self.pokes += 1


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = Clock()
        self.store = JsonStore(Path(tmp.name) / "agent.json")
        self.dispatcher = FakeDispatcher()
        self.source = FakeSource()

    def agent(self, **kwargs):
        kwargs.setdefault("source", self.source)
        return Agent(
            name="Stan",
            tracker=PowerTracker(debounce=10),
            dispatcher=self.dispatcher,
            formatter=Formatter(timezone.utc, self.clock),
            store=self.store,
            clock=self.clock,
            monotonic=self.clock,
            **kwargs,
        )

    def tick(self, agent, seconds):
        """Poll once a second for `seconds` seconds."""
        end = self.clock.now + seconds
        while self.clock.now < end:
            agent.step()
            self.clock.advance(1)


class PowerEventsTest(AgentTestCase):
    def test_announces_the_loss_and_the_return_of_power(self):
        agent = self.agent()
        agent.start()
        self.tick(agent, 30)
        self.assertEqual(self.dispatcher.titles, [])

        self.source.reading = False
        self.tick(agent, 12)
        self.assertEqual(self.dispatcher.titles, ["⚡ Nestala struja: Stan"])
        self.tick(agent, 50)
        self.assertEqual(len(self.dispatcher.messages), 1)  # announced once, not on every poll

        self.source.reading = True
        self.tick(agent, 12)
        self.assertEqual(self.dispatcher.titles[1], "💡 Struja se vratila: Stan")
        self.assertIn("1 min 2 s", self.dispatcher.messages[1].body)  # 12 + 50 s on battery

    def test_a_flicker_is_not_announced(self):
        agent = self.agent()
        agent.start()
        self.source.reading = False
        self.tick(agent, 5)
        self.source.reading = True
        self.tick(agent, 30)
        self.assertEqual(self.dispatcher.titles, [])

    def test_exposes_the_raw_state_and_pokes_the_heartbeat_on_change(self):
        heartbeat = FakeHeartbeat()
        agent = self.agent(heartbeat=heartbeat)
        self.assertEqual(agent.power_state, "unknown")
        agent.step()
        self.assertEqual((agent.power_state, heartbeat.pokes), ("mains", 1))
        agent.step()
        self.assertEqual(heartbeat.pokes, 1)
        self.source.reading = False
        agent.step()  # still inside the debounce, but the watchdog should already hear about it
        self.assertEqual((agent.power_state, heartbeat.pokes), ("battery", 2))
        self.assertEqual(self.dispatcher.titles, [])

    def test_unreadable_source_is_reported_as_unknown(self):
        self.source.reading = None
        agent = self.agent()
        agent.step()
        self.assertEqual(agent.power_state, "unknown")

    def test_without_a_source_nothing_is_announced(self):
        agent = self.agent(source=None)
        agent.start()
        self.tick(agent, 100)
        self.assertEqual(self.dispatcher.titles, [])
        self.assertEqual(agent.power_state, "unknown")


class StateTest(AgentTestCase):
    def test_start_marks_the_run_as_in_progress(self):
        self.agent().start()
        self.assertEqual(self.store.load(), {"last_seen": self.clock.now, "clean": False, "outage": False})

    def test_the_state_remembers_that_an_outage_is_going_on(self):
        agent = self.agent()
        agent.start()
        self.source.reading = False
        self.tick(agent, 12)
        agent.stop()
        self.assertEqual(self.store.load()["outage"], True)

    def test_liveness_is_written_periodically(self):
        agent = self.agent()
        agent.start()
        started = self.clock.now
        self.clock.advance(STATE_INTERVAL - 1)
        agent.step()
        self.assertEqual(self.store.load()["last_seen"], started)
        self.clock.advance(1)
        agent.step()
        self.assertEqual(self.store.load()["last_seen"], self.clock.now)

    def test_clean_stop_is_recorded(self):
        agent = self.agent()
        agent.start()
        agent.stop()
        self.assertTrue(self.store.load()["clean"])

    def test_stop_with_undelivered_messages_stays_unclean(self):
        # otherwise the next start would not report the outage whose alert never got out
        agent = self.agent()
        agent.start()
        self.dispatcher.waiting = 1
        agent.stop()
        self.assertFalse(self.store.load()["clean"])


class UncleanRestartTest(AgentTestCase):
    def previous_run(self, *, ago, clean):
        self.store.save({"last_seen": self.clock.now - ago, "clean": clean})

    def test_a_long_unclean_gap_is_reported_as_a_probable_outage(self):
        self.previous_run(ago=3600, clean=False)
        self.agent().start()
        [message] = self.dispatcher.messages
        self.assertEqual(message.title, "🔌 Uređaj je bio ugašen: Stan")
        self.assertIn("1 h", message.body)

    def test_a_clean_stop_is_not_an_outage(self):
        self.previous_run(ago=3600, clean=True)
        self.agent().start()
        self.assertEqual(self.dispatcher.titles, [])

    def test_a_quick_restart_after_a_crash_is_not_an_outage(self):
        self.previous_run(ago=MIN_GAP - 1, clean=False)
        self.agent().start()
        self.assertEqual(self.dispatcher.titles, [])

    def test_the_first_run_is_silent(self):
        self.agent().start()
        self.assertEqual(self.dispatcher.titles, [])

    def test_a_clock_that_went_backwards_is_ignored(self):
        self.previous_run(ago=-3600, clean=False)
        self.agent().start()
        self.assertEqual(self.dispatcher.titles, [])

    def test_a_damaged_state_file_is_ignored(self):
        self.store.path.write_text("{ not json")
        with self.assertLogs("powerout.store", "WARNING"):
            self.agent().start()
        self.assertEqual(self.dispatcher.titles, [])

    def test_a_state_without_a_timestamp_is_ignored(self):
        self.store.save({"clean": False})
        self.agent().start()
        self.assertEqual(self.dispatcher.titles, [])

    def test_the_report_is_made_only_once(self):
        self.previous_run(ago=3600, clean=False)
        self.agent().start()
        self.agent().start()  # the first start overwrote the state
        self.assertEqual(len(self.dispatcher.messages), 1)


class RestartAfterOutageTest(AgentTestCase):
    """A laptop shuts itself down cleanly when its battery runs low; nobody would learn the power is back."""

    def shut_down_during_an_outage(self):
        agent = self.agent()
        agent.start()
        self.source.reading = False
        self.tick(agent, 12)
        agent.stop()
        self.dispatcher.messages.clear()

    def test_the_next_start_reports_it(self):
        self.shut_down_during_an_outage()
        self.clock.advance(3 * 3600)
        self.agent().start()
        [message] = self.dispatcher.messages
        self.assertEqual(message.title, "🔌 Uređaj je ponovo pokrenut: Stan")
        self.assertIn("3 h", message.body)

    def test_a_clean_stop_while_the_power_was_fine_is_silent(self):
        agent = self.agent()
        agent.start()
        self.tick(agent, 12)
        agent.stop()
        self.clock.advance(3600)
        self.agent().start()
        self.assertEqual(self.dispatcher.titles, [])

    def test_restarting_the_service_during_an_outage_is_silent(self):
        self.shut_down_during_an_outage()
        self.clock.advance(MIN_GAP - 1)
        self.agent().start()
        self.assertEqual(self.dispatcher.titles, [])


class RunTest(AgentTestCase):
    def test_run_polls_until_stopped_and_then_records_a_clean_stop(self):
        stop = threading.Event()

        class Source(FakeSource):
            reads = 0

            def read(self):
                Source.reads += 1
                stop.set()
                return True

        self.agent(source=Source(), poll_seconds=0.01).run(stop)
        self.assertEqual(Source.reads, 1)
        self.assertTrue(self.store.load()["clean"])

    def test_run_survives_a_failing_source(self):
        stop = threading.Event()

        class Source(FakeSource):
            reads = 0

            def read(self):
                Source.reads += 1
                if Source.reads == 1:
                    raise RuntimeError("sensor glitch")
                stop.set()
                return True

        with self.assertLogs("powerout.agent", "ERROR"):
            self.agent(source=Source(), poll_seconds=0.01).run(stop)
        self.assertEqual(Source.reads, 2)


class HeartbeatSenderTest(unittest.TestCase):
    def sender(self, url, token="", power="mains", interval=60.0):
        return HeartbeatSender(Heartbeat(url=url, token=token, interval_seconds=interval), "Stan", lambda: power)

    def test_posts_the_name_and_the_power_state_with_the_token(self):
        with StubServer() as server:
            self.assertTrue(self.sender(server.url + "/heartbeat", token="s3cret", power="battery").beat())
        [request] = server.requests
        self.assertEqual((request.method, request.path), ("POST", "/heartbeat"))
        self.assertEqual(request.json(), {"name": "Stan", "power": "battery"})
        self.assertEqual(request.headers["Authorization"], "Bearer s3cret")

    def test_no_token_means_no_authorization_header(self):
        with StubServer() as server:
            self.sender(server.url).beat()
        self.assertNotIn("Authorization", server.requests[0].headers)

    def test_failures_are_logged_once_per_streak_and_recovery_is_noted(self):
        status = [500]
        with StubServer(lambda r: (status[0], {})) as server:
            sender = self.sender(server.url)
            with self.assertLogs("powerout.agent", "INFO") as logs:
                self.assertFalse(sender.beat())
                self.assertFalse(sender.beat())
                status[0] = 200
                self.assertTrue(sender.beat())
        self.assertEqual([r.levelname for r in logs.records], ["WARNING", "INFO"])
        self.assertIn("works again", logs.records[1].getMessage())

    def test_an_unreachable_server_is_a_failed_beat_not_an_exception(self):
        with self.assertLogs("powerout.agent", "WARNING"):
            self.assertFalse(self.sender("http://127.0.0.1:9/heartbeat").beat())

    def test_poke_sends_the_next_beat_without_waiting_for_the_interval(self):
        with StubServer() as server:
            sender = self.sender(server.url, interval=60)
            thread = threading.Thread(target=sender.run, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 2)
            self.addCleanup(sender.stop)
            self.assertTrue(wait_until(lambda: len(server.requests) == 1))
            sender.poke()
            self.assertTrue(wait_until(lambda: len(server.requests) == 2))

    def test_stop_ends_the_loop_promptly(self):
        with StubServer() as server:
            sender = self.sender(server.url, interval=60)
            thread = threading.Thread(target=sender.run, daemon=True)
            thread.start()
            self.assertTrue(wait_until(lambda: len(server.requests) == 1))
            sender.stop()
            thread.join(2)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
