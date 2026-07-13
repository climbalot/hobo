"""Inbound fill admission: the single gate every fill passes through, whatever its
source (execution WS or REST reconciler). The one check is dedup by `trade_id` - a
fill is admitted exactly once - with the seen set seeded from the log at startup so
replayed fills aren't re-admitted.
"""

from __future__ import annotations

from collections.abc import Callable

from hobo.core.events import FillEvent
from hobo.log import reader
from hobo.log.events import EventType


class FillIngestor:
    def __init__(self, publish: Callable[[FillEvent], None]) -> None:
        self._publish = publish
        self._seen: set[str] = set()
        self._last_fill_ns = 0  # record time of the newest fill in the log at seed time

    def seed_from_log(self, log_path: str) -> None:
        for envelope in reader.replay(reader.open_readonly(log_path)):
            if envelope.event_type == EventType.FILL:
                trade_id = envelope.payload.get("trade_id")
                if trade_id:  # a real exchange fill (synthetic net-patch fills have none)
                    self._seen.add(trade_id)
                    self._last_fill_ns = max(self._last_fill_ns, envelope.ts_ns)

    def last_fill_ns(self) -> int:
        """Record time of the newest real (trade_id-bearing) fill, 0 if none - the fill
        reconciler baselines off this. Synthetic net-patch fills (no trade_id) don't
        count, keeping the reconciler forward-only after a patch."""
        return self._last_fill_ns

    def seen_copy(self) -> set[str]:
        """Snapshot of admitted trade_ids - copied on the loop so the REST reconciler
        can page in a thread against a frozen view without racing the live set."""
        return set(self._seen)

    def submit(self, fill: FillEvent) -> bool:
        """Admit a fill (publish it) unless it fails a check. Returns False if
        dropped. `trade_id`-less fills (e.g. paper) are always admitted."""
        if fill.trade_id and fill.trade_id in self._seen:
            return False  # duplicate: already admitted from another source
        if fill.trade_id:
            self._seen.add(fill.trade_id)
        self._publish(fill)
        return True
