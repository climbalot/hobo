"""FeedHealthMonitor: ties market-data freshness to the desk kill switch via a
StalenessWatchdog - `on_message` records ticks, periodic `check()` trips the kill
switch on fresh->stale and clears it when ticks resume. Fails closed, because
approving orders against a stale mark is a correctness bug.
"""

from __future__ import annotations

from hobo.core.events import Event
from hobo.log.events import KillSwitch
from hobo.risk.staleness import StalenessWatchdog
from hobo.core.state_store import StateStore

FEED_STALE_REASON = "feed stale"


class FeedHealthMonitor:
    def __init__(self, store: StateStore, watchdog: StalenessWatchdog, desk_id: str) -> None:
        self._store = store
        self._watchdog = watchdog
        self._desk_id = desk_id
        # Feed-stale kill switch persists in the snapshot but the watchdog restarts fresh,
        # so seed it stale to match: the first resumed tick clears the switch (a dead feed keeps it set).
        desk = store.state.desk
        if desk.kill_switch and desk.kill_switch_reason == FEED_STALE_REASON:
            self._watchdog.stale = True

    def on_message(self, event: Event) -> None:
        if self._watchdog.record_message():  # cleared a stale state
            self._toggle(enabled=False, reason="")

    def check(self) -> None:
        if self._watchdog.check():  # transitioned fresh -> stale
            self._toggle(enabled=True, reason=FEED_STALE_REASON)

    def _toggle(self, enabled: bool, reason: str) -> None:
        self._store.record(KillSwitch("DESK", self._desk_id, enabled, reason))
