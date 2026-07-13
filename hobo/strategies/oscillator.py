"""Oscillator toy strategy: a multi-instrument activity generator. Places a small
order every `interval_ticks` marks, oscillating inventory within +/- `position_band`
around flat to produce continuous two-sided flow for exercising the pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable

from hobo.core.events import Action, Event, FillEvent, MarkEvent, PlaceOrder
from hobo.risk.model import Side
from hobo.risk.state import State

# A no-edge activity generator: every round-trip pays taker fees + spread, so it
# structurally bleeds fees. Kept infrequent so it's demo activity, not a fee firehose.
DEFAULT_INTERVAL_TICKS = 30
DEFAULT_ORDER_QTY = 0.1
DEFAULT_POSITION_BAND = 1.0


class OscillatorStrategy:
    def __init__(
        self,
        instrument_ids: Iterable[str],
        interval_ticks: int = DEFAULT_INTERVAL_TICKS,
        order_qty: float = DEFAULT_ORDER_QTY,
        position_band: float = DEFAULT_POSITION_BAND,
    ) -> None:
        self.instrument_ids = set(instrument_ids)
        self.interval_ticks = interval_ticks
        self.order_qty = order_qty
        self.position_band = position_band
        self._position: dict[str, float] = {}
        self._ticks: dict[str, int] = {}
        self._last_side: dict[str, Side] = {}

    def on_event(self, event: Event, state: State) -> list[Action]:
        if isinstance(event, FillEvent):
            signed = event.qty if event.side == Side.BUY.value else -event.qty
            self._position[event.instrument_id] = self._position.get(event.instrument_id, 0.0) + signed
            return []

        if not isinstance(event, MarkEvent) or event.instrument_id not in self.instrument_ids:
            return []

        iid = event.instrument_id
        self._ticks[iid] = self._ticks.get(iid, 0) + 1
        if self._ticks[iid] % self.interval_ticks != 0:
            return []

        # Buy when short of the band, sell when long of it, else flip - keeps
        # inventory bounded while always trading both sides.
        pos = self._position.get(iid, 0.0)
        if pos >= self.position_band:
            side = Side.SELL
        elif pos <= -self.position_band:
            side = Side.BUY
        else:
            side = Side.BUY if self._last_side.get(iid, Side.SELL) == Side.SELL else Side.SELL
        self._last_side[iid] = side
        return [PlaceOrder(instrument_id=iid, side=side, qty=self.order_qty)]
