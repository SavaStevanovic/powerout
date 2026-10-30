"""A tiny JSON file store. Writes are atomic because power can vanish at any moment, which is the whole point."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self._failing = False

    def load(self) -> dict | None:
        """The stored object, or None if there is none (or it is unreadable)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable state file %s: %s", self.path, e)
            return None
        return data if isinstance(data, dict) else None

    def save(self, data: dict) -> None:
        """Replace the stored object. Failures are logged (once per streak) and never raised."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            if not self._failing:
                log.warning("cannot write state file %s: %s", self.path, e)
            self._failing = True
        else:
            self._failing = False
