"""Recorder: bus subscriber that persists state-mutating events, mapping each
runtime event to its log payload via a dispatch table. Subscribed before the
StrategyRunner so state is current before strategies react.
"""

from __future__ import annotations

from collections.abc import Callable

from hobo.core.events import Event, FillEvent, FundingEvent, MarkEvent
from hobo.core.state_store import StateStore
from hobo.log.events import Fill, FundingUpdate, LogEvent, MarkUpdate

# runtime event type -> its log payload
_LOG_MAPPERS: dict[type, Callable[[Event], LogEvent]] = {
    MarkEvent: lambda e: MarkUpdate(e.instrument_id, e.mark_price, e.ts_ns),
    FundingEvent: lambda e: FundingUpdate(e.instrument_id, e.funding_rate, e.funding_time_ns),
    FillEvent: lambda e: Fill(e.order_id, e.book_id, e.instrument_id, e.side, e.qty, e.fill_price, e.fee, e.trade_id),
}


class Recorder:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    def record(self, event: Event) -> None:
        mapper = _LOG_MAPPERS.get(type(event))
        if mapper is not None:
            self._store.record(mapper(event))
