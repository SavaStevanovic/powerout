"""Local detection of mains power: sources that read the state, and a tracker that debounces it."""

from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import ConfigError, Power

log = logging.getLogger(__name__)

SYSFS_ROOT = Path("/sys/class/power_supply")
# Supply types that mean "something external is powering this machine".
EXTERNAL_TYPES = frozenset(
    {"mains", "usb", "usb_dcp", "usb_cdp", "usb_aca", "usb_c", "usb_pd", "usb_pd_drp", "brickid"}
)


class PowerSource(Protocol):
    description: str

    def read(self) -> bool | None:
        """True = mains power present, False = running on battery, None = cannot tell."""


@dataclass(frozen=True)
class Supply:
    name: str
    kind: str  # the kernel's "type", lower-cased
    scope: str  # "system" or "device" (batteries of mice and keyboards are "device")
    online: bool | None


def _attr(directory: Path, name: str) -> str | None:
    try:
        return (directory / name).read_text().strip()
    except OSError:
        return None


class SysfsSource:
    """Reads /sys/class/power_supply: the AC adapter of a laptop, or anything the kernel exposes."""

    def __init__(self, root: Path = SYSFS_ROOT, supply: str = ""):
        self._root = root
        self._pinned = supply
        self.description = f"sysfs ({supply})" if supply else "sysfs (adapter + baterija)"

    def supplies(self) -> list[Supply]:
        try:
            directories = sorted(p for p in self._root.iterdir() if p.is_dir())
        except OSError:
            return []
        found = []
        for directory in directories:
            online = {"1": True, "0": False}.get(_attr(directory, "online") or "")
            found.append(
                Supply(
                    directory.name,
                    (_attr(directory, "type") or "").lower(),
                    (_attr(directory, "scope") or "system").lower(),
                    online,
                )
            )
        return found

    def usable(self) -> bool:
        """Whether reading this makes sense on this machine.

        "No external power" only means something when the machine can run without it, so the
        automatic mode insists on a system battery. A desktop with a USB-C port that always
        reports "offline" must not be mistaken for a permanent outage.
        """
        supplies = self.supplies()
        if self._pinned:
            return any(s.name == self._pinned for s in supplies)
        has_external = any(s.kind in EXTERNAL_TYPES for s in supplies)
        has_battery = any(s.kind == "battery" and s.scope != "device" for s in supplies)
        return has_external and has_battery

    def read(self) -> bool | None:
        supplies = self.supplies()
        if self._pinned:
            online = [s.online for s in supplies if s.name == self._pinned]
        else:
            online = [s.online for s in supplies if s.kind in EXTERNAL_TYPES]
        if any(state is True for state in online):
            return True
        if online and all(state is False for state in online):
            return False
        return None


class NutSource:
    """Asks Network UPS Tools (`upsc`) whether the UPS is on line power ("OL") or on battery ("OB")."""

    def __init__(self, ups: str, binary: str = "upsc", timeout: float = 5.0):
        self._argv = [binary, ups, "ups.status"]
        self._timeout = timeout
        self.description = f"NUT ({ups})"

    def read(self) -> bool | None:
        try:
            result = subprocess.run(self._argv, capture_output=True, text=True, timeout=self._timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        flags = result.stdout.split() if result.returncode == 0 else []
        if "OB" in flags:
            return False
        if "OL" in flags:
            return True
        return None


class CommandSource:
    """Runs a user-supplied command: exit code 0 = mains, 1 = on battery, anything else = unknown."""

    def __init__(self, command: str, timeout: float = 10.0):
        self._argv = shlex.split(command)
        self._timeout = timeout
        self.description = f"komanda ({command})"

    def read(self) -> bool | None:
        try:
            result = subprocess.run(
                self._argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self._timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return {0: True, 1: False}.get(result.returncode)


def detect_source(cfg: Power, sysfs_root: Path = SYSFS_ROOT) -> PowerSource | None:
    """The configured power source, or None if this machine has nothing to read (e.g. a desktop without UPS)."""
    kind = cfg.source
    if kind == "none":
        return None
    if kind == "auto":
        kind = "nut" if cfg.ups else "command" if cfg.command else "sysfs"
    if kind == "nut":
        return NutSource(cfg.ups)
    if kind == "command":
        return CommandSource(cfg.command)
    source = SysfsSource(sysfs_root, cfg.supply)
    if source.usable():
        return source
    if cfg.source == "sysfs":  # asked for explicitly, so do not silently degrade
        raise ConfigError("[power] source = \"sysfs\", ali u /sys/class/power_supply nema upotrebljivog napajanja")
    return None


@dataclass(frozen=True)
class PowerEvent:
    kind: str  # "lost" or "restored"
    at: float  # wall-clock time the new state was first seen
    duration: float | None = None  # restored only: how long the outage lasted, in seconds


class PowerTracker:
    """Turns raw readings into confirmed power-lost / power-restored events.

    A change counts only once it has lasted `debounce` seconds, so a flicker of the grid does
    not produce a pair of messages. The event carries the time the change was first seen.
    Mains power is assumed at the start, so an agent started on battery reports a loss.
    """

    def __init__(self, debounce: float):
        self.debounce = debounce
        self.mains = True  # the confirmed state
        self._candidate: tuple[float, float] | None = None  # (monotonic, wall) of the first differing reading
        self._lost_wall: float | None = None  # when the confirmed outage began

    def observe(self, reading: bool | None, mono: float, wall: float) -> list[PowerEvent]:
        if reading is None:
            return []
        if reading == self.mains:
            self._candidate = None
            return []
        if self._candidate is None:
            self._candidate = (mono, wall)
        first_mono, first_wall = self._candidate
        if mono - first_mono < self.debounce:
            return []
        self._candidate = None
        self.mains = reading
        if not reading:
            self._lost_wall = first_wall
            return [PowerEvent("lost", first_wall)]
        # The length of an outage is measured on the wall clock: unlike the monotonic one it keeps
        # running while a laptop sleeps, and a long outage is exactly when a laptop would.
        duration = max(0.0, first_wall - self._lost_wall) if self._lost_wall is not None else 0.0
        self._lost_wall = None
        return [PowerEvent("restored", first_wall, duration)]
