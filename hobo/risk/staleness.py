"""Feed staleness watchdog. OKX's mark-price channel carries no sequence number, so
gaps aren't detectable - only staleness (time since last tick) is. A pure,
clock-injectable class; sustained staleness trips the desk kill switch, because
approving orders against a stale mark is a correctness bug.
"""

from __future__ import annotations

import time
from collections.abc import Callable

DEFAULT_STALENESS_THRESHOLD_S = 5.0


class StalenessWatchdog:
    def __init__(
        self,
        threshold_s: float = DEFAULT_STALENESS_THRESHOLD_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold_s = threshold_s
        self._clock = clock
        self._last_seen: float | None = None
        self.stale = False

    def record_message(self) -> bool:
        """Call whenever a message arrives. Returns True if this cleared a stale state."""
        self._last_seen = self._clock()
        was_stale = self.stale
        self.stale = False
        return was_stale

    def check(self) -> bool:
        """Call periodically. Returns True if this transitioned fresh -> stale."""
        if self._last_seen is None or self.stale:
            return False
        age = self._clock() - self._last_seen
        if age >= self._threshold_s:
            self.stale = True
            return True
        return False

    def age(self) -> float | None:
        if self._last_seen is None:
            return None
        return self._clock() - self._last_seen
