"""Background delivery of notifications, with retries."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from .notify import Message, Notifier

log = logging.getLogger(__name__)

RETRY_DELAYS = (5.0, 15.0, 30.0, 60.0)  # the last pause repeats
LATE_AFTER = 60.0  # a message older than this when finally sent is marked as delayed
MAX_PENDING = 100  # the oldest messages are dropped beyond this


@dataclass
class _Item:
    message: Message
    created: float


class Dispatcher:
    """Delivers messages in order from a background thread.

    While every channel fails (typically the router has no power during an outage) the oldest
    message is retried with growing pauses, so an alert arrives late instead of never.
    """

    def __init__(
        self,
        notifier: Notifier,
        *,
        decorate: Callable[[Message, float], Message] | None = None,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._notifier = notifier
        self._decorate = decorate
        self._retry_delays = retry_delays
        self._clock = clock
        self._queue: deque[_Item] = deque(maxlen=MAX_PENDING)
        self._cond = threading.Condition()
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="dispatcher", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def submit(self, message: Message) -> None:
        with self._cond:
            self._queue.append(_Item(message, self._clock()))
            self._cond.notify_all()

    def pending(self) -> int:
        with self._cond:
            return len(self._queue)

    def _run(self) -> None:
        failures = 0
        while True:
            with self._cond:
                while not self._queue and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                item = self._queue[0]
            if self._deliver(item):
                failures = 0
                with self._cond:
                    if self._queue and self._queue[0] is item:  # not dropped meanwhile
                        self._queue.popleft()
                continue
            delay = self._retry_delays[min(failures, len(self._retry_delays) - 1)]
            failures += 1
            log.info("will retry in %.0f s (%d message(s) waiting)", delay, self.pending())
            with self._cond:
                if not self._stopping:
                    self._cond.wait(delay)

    def _deliver(self, item: _Item) -> bool:
        message = item.message
        waited = self._clock() - item.created
        if waited >= LATE_AFTER and self._decorate:
            message = self._decorate(message, waited)
        try:
            return self._notifier.send(message)
        except Exception:  # a bug in a channel must not kill the delivery thread
            log.exception("unexpected error while sending %r", message.title)
            return False
