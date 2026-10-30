"""The agent: runs on a device at the monitored location."""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit

from .config import Config, Heartbeat
from .dispatch import Dispatcher
from .messages import Formatter
from .notify import build_notifier
from .power import PowerEvent, PowerSource, PowerTracker, detect_source
from .store import JsonStore

log = logging.getLogger(__name__)

STATE_INTERVAL = 30.0  # how often "I am still alive" is written to disk
# A restart after a shorter gap is a crash and a quick restart by the service manager, not an outage.
# It must stay above STATE_INTERVAL plus the restart delay, yet below the boot time of a small board.
MIN_GAP = 60.0
HEARTBEAT_TIMEOUT = 10.0
POWER_NAMES = {True: "mains", False: "battery", None: "unknown"}


class HeartbeatSender:
    """Tells the watchdog (or any ping URL, e.g. healthchecks.io) that this device is alive."""

    def __init__(self, cfg: Heartbeat, name: str, power: Callable[[], str]):
        self._cfg = cfg
        self._name = name
        self._power = power
        self._host = urlsplit(cfg.url).hostname
        self._healthy = True
        self._wake = threading.Event()
        self._stopping = False

    def beat(self) -> bool:
        headers = {"Content-Type": "application/json"}
        if self._cfg.token:
            headers["Authorization"] = f"Bearer {self._cfg.token}"
        request = urllib.request.Request(
            self._cfg.url,
            data=json.dumps({"name": self._name, "power": self._power()}).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=HEARTBEAT_TIMEOUT) as response:
                response.read()
        except urllib.error.HTTPError as e:
            e.close()
            failure = f"HTTP {e.code}"
        except (OSError, http.client.HTTPException) as e:
            failure = str(e)
        else:
            failure = None
        if failure:
            if self._healthy:
                log.warning("heartbeat to %s failed: %s", self._host, failure)
            self._healthy = False
            return False
        if not self._healthy:
            log.info("heartbeat to %s works again", self._host)
        self._healthy = True
        return True

    def poke(self) -> None:
        """Send the next heartbeat now instead of waiting for the interval (e.g. the power state changed)."""
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    def run(self) -> None:
        while not self._stopping:
            self._wake.clear()
            try:
                self.beat()
            except Exception:
                log.exception("heartbeat failed unexpectedly")
            self._wake.wait(self._cfg.interval_seconds)


class Agent:
    def __init__(
        self,
        *,
        name: str,
        source: PowerSource | None,
        tracker: PowerTracker,
        dispatcher: Dispatcher,
        formatter: Formatter,
        store: JsonStore,
        poll_seconds: float = 2.0,
        heartbeat: HeartbeatSender | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._name = name
        self._source = source
        self._tracker = tracker
        self._dispatcher = dispatcher
        self._formatter = formatter
        self._store = store
        self._poll = poll_seconds
        self._heartbeat = heartbeat
        self._clock = clock
        self._monotonic = monotonic
        self._saved_at = 0.0
        self.power_state = "unknown"  # the latest raw reading: mains / battery / unknown

    def start(self) -> None:
        """Report what happened while the agent was not running, then mark this run as in progress."""
        previous = self._store.load() or {}
        now = self._clock()
        since = previous.get("last_seen")
        if isinstance(since, (int, float)) and now - since >= MIN_GAP:
            if previous.get("clean") is False:
                log.info("previous run ended without a clean stop %.0f s ago", now - since)
                self._dispatcher.submit(self._formatter.unexpected_restart(self._name, since, now))
            elif previous.get("outage"):
                # e.g. a laptop that shut itself down cleanly when its battery ran low
                log.info("previous run was shut down during an outage %.0f s ago", now - since)
                self._dispatcher.submit(self._formatter.restart_after_outage(self._name, since, now))
        self._save(clean=False)

    def step(self) -> None:
        """Poll the power source once."""
        mono, wall = self._monotonic(), self._clock()
        if self._source is not None:
            reading = self._source.read()
            state = POWER_NAMES[reading]
            if state != self.power_state:
                self.power_state = state
                if self._heartbeat:
                    self._heartbeat.poke()  # the watchdog should hear about it right away
            for event in self._tracker.observe(reading, mono, wall):
                self._announce(event)
        if wall - self._saved_at >= STATE_INTERVAL:
            self._save(clean=False)

    def stop(self) -> None:
        # Messages that never got out must not be forgotten: leave the run "unclean" so that the
        # next start reports the gap instead.
        self._save(clean=self._dispatcher.pending() == 0)

    def run(self, stop: threading.Event) -> None:
        self.start()
        interval = self._poll if self._source else STATE_INTERVAL
        try:
            while not stop.is_set():
                try:
                    self.step()
                except Exception:
                    log.exception("polling failed")
                stop.wait(interval)
        finally:
            self.stop()

    def _announce(self, event: PowerEvent) -> None:
        if event.kind == "lost":
            log.warning("power lost")
            message = self._formatter.power_lost(self._name, event.at)
        else:
            log.warning("power restored after %.0f s", event.duration)
            message = self._formatter.power_restored(self._name, event.at, event.duration or 0.0)
        self._dispatcher.submit(message)

    def _save(self, *, clean: bool) -> None:
        self._saved_at = self._clock()
        self._store.save({"last_seen": self._saved_at, "clean": clean, "outage": not self._tracker.mains})


def run_agent(cfg: Config, stop: threading.Event) -> None:
    formatter = Formatter(cfg.general.tz)
    dispatcher = Dispatcher(build_notifier(cfg), decorate=formatter.delayed)
    source = detect_source(cfg.power)
    sender = None
    if cfg.heartbeat.url:
        # the sender and the agent need each other; the lambda looks `agent` up only when a heartbeat is sent
        sender = HeartbeatSender(cfg.heartbeat, cfg.general.name, lambda: agent.power_state)
    agent = Agent(
        name=cfg.general.name,
        source=source,
        tracker=PowerTracker(cfg.power.debounce_seconds),
        dispatcher=dispatcher,
        formatter=formatter,
        store=JsonStore(cfg.general.state_path / "agent.json"),
        poll_seconds=cfg.power.poll_seconds,
        heartbeat=sender,
    )
    if source is None:
        log.warning("no local power source (a desktop without battery/UPS?); only heartbeats and restarts are watched")
    log.info(
        "agent %r started: power source=%s, heartbeat=%s",
        cfg.general.name,
        source.description if source else "none",
        urlsplit(cfg.heartbeat.url).hostname if sender else "off",
    )
    dispatcher.start()
    if sender:
        threading.Thread(target=sender.run, name="heartbeat", daemon=True).start()
    try:
        agent.run(stop)
    finally:
        if sender:
            sender.stop()
        dispatcher.stop()
