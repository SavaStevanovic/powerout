import threading
import time
import unittest

from powerout.dispatch import Dispatcher
from powerout.notify import Message
from tests.helpers import Clock, wait_until


class ScriptedNotifier:
    """Fails the first `failures` sends, then delivers."""

    def __init__(self, failures=0, raises=False):
        self.failures = failures
        self.raises = raises
        self.sent: list[Message] = []
        self.attempts = 0
        self._cond = threading.Condition()

    def send(self, message):
        with self._cond:
            self.attempts += 1
            if self.failures:
                self.failures -= 1
                if self.raises:
                    raise RuntimeError("boom")
                return False
            self.sent.append(message)
            self._cond.notify_all()
            return True

    def wait_for(self, count, timeout=5.0):
        with self._cond:
            return self._cond.wait_for(lambda: len(self.sent) >= count, timeout)


def started(notifier, **kwargs):
    kwargs.setdefault("retry_delays", (0.01,))
    dispatcher = Dispatcher(notifier, **kwargs)
    dispatcher.start()
    return dispatcher


class DispatcherTest(unittest.TestCase):
    def make(self, notifier, **kwargs):
        dispatcher = started(notifier, **kwargs)
        self.addCleanup(dispatcher.stop)
        return dispatcher

    def test_delivers_in_order(self):
        notifier = ScriptedNotifier()
        dispatcher = self.make(notifier)
        for title in "abc":
            dispatcher.submit(Message(title))
        self.assertTrue(notifier.wait_for(3))
        self.assertEqual([m.title for m in notifier.sent], ["a", "b", "c"])

    def test_retries_until_a_channel_works(self):
        notifier = ScriptedNotifier(failures=3)
        dispatcher = self.make(notifier)
        dispatcher.submit(Message("alert"))
        self.assertTrue(notifier.wait_for(1))
        self.assertEqual(notifier.attempts, 4)
        self.assertTrue(wait_until(lambda: dispatcher.pending() == 0))

    def test_keeps_the_order_while_the_first_message_is_stuck(self):
        notifier = ScriptedNotifier(failures=2)
        dispatcher = self.make(notifier)
        dispatcher.submit(Message("lost"))
        dispatcher.submit(Message("restored"))
        self.assertTrue(notifier.wait_for(2))
        self.assertEqual([m.title for m in notifier.sent], ["lost", "restored"])

    def test_a_message_waits_in_the_queue_while_undeliverable(self):
        notifier = ScriptedNotifier(failures=10**6)
        dispatcher = self.make(notifier, retry_delays=(0.05,))
        dispatcher.submit(Message("alert"))
        self.assertTrue(wait_until(lambda: notifier.attempts >= 2))
        self.assertEqual(dispatcher.pending(), 1)

    def test_an_exception_in_a_channel_does_not_kill_the_thread(self):
        notifier = ScriptedNotifier(failures=1, raises=True)
        dispatcher = self.make(notifier)
        with self.assertLogs("powerout.dispatch", "ERROR"):
            dispatcher.submit(Message("alert"))
            self.assertTrue(notifier.wait_for(1))

    def test_late_messages_are_marked(self):
        clock = Clock(0.0)
        notifier = ScriptedNotifier()
        dispatcher = Dispatcher(
            notifier, decorate=lambda m, delay: Message(m.title, f"late by {delay:.0f}"), clock=clock
        )
        dispatcher.submit(Message("alert", "body"))
        clock.advance(120)
        dispatcher.start()
        self.addCleanup(dispatcher.stop)
        self.assertTrue(notifier.wait_for(1))
        self.assertEqual(notifier.sent[0], Message("alert", "late by 120"))

    def test_prompt_messages_are_left_alone(self):
        clock = Clock(0.0)
        notifier = ScriptedNotifier()
        dispatcher = Dispatcher(notifier, decorate=lambda m, delay: Message("decorated"), clock=clock)
        dispatcher.submit(Message("alert"))
        clock.advance(5)
        dispatcher.start()
        self.addCleanup(dispatcher.stop)
        self.assertTrue(notifier.wait_for(1))
        self.assertEqual(notifier.sent[0].title, "alert")

    def test_decoration_does_not_pile_up_over_retries(self):
        clock = Clock(0.0)
        notifier = ScriptedNotifier(failures=2)
        dispatcher = Dispatcher(
            notifier, decorate=lambda m, delay: Message(m.title, m.body + "!"), retry_delays=(0.01,), clock=clock
        )
        dispatcher.submit(Message("alert", "body"))
        clock.advance(120)
        dispatcher.start()
        self.addCleanup(dispatcher.stop)
        self.assertTrue(notifier.wait_for(1))
        self.assertEqual(notifier.sent[0].body, "body!")

    def test_stop_does_not_wait_for_the_next_retry(self):
        notifier = ScriptedNotifier(failures=10**6)
        dispatcher = started(notifier, retry_delays=(60.0,))
        dispatcher.submit(Message("alert"))
        self.assertTrue(wait_until(lambda: notifier.attempts >= 1))
        began = time.monotonic()
        dispatcher.stop()
        self.assertLess(time.monotonic() - began, 1.0)

    def test_the_queue_is_bounded(self):
        notifier = ScriptedNotifier(failures=10**6)
        dispatcher = Dispatcher(notifier)  # not started, so nothing is consumed
        for i in range(250):
            dispatcher.submit(Message(str(i)))
        self.assertEqual(dispatcher.pending(), 100)


if __name__ == "__main__":
    unittest.main()
