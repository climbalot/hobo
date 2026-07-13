"""Momentum toy strategy (multi-instrument): when the move since the previous tick
exceeds a bps threshold, emits a fixed-size order in the move's direction.
"""

from __future__ import annotations

from collections.abc import Iterable

from hobo.core.events import Action, Event, MarkEvent, PlaceOrder
from hobo.risk.model import Side
from hobo.risk.state import State

DEFAULT_THRESHOLD_BPS = 5.0
DEFAULT_ORDER_QTY = 1.0


class MomentumStrategy:
    def __init__(
        self,
        instrument_ids: Iterable[str],
        threshold_bps: float = DEFAULT_THRESHOLD_BPS,
        order_qty: float = DEFAULT_ORDER_QTY,
    ) -> None:
        self.instrument_ids = set(instrument_ids)
        self.threshold_bps = threshold_bps
        self.order_qty = order_qty
        self._prev_mark: dict[str, float] = {}

    def on_event(self, event: Event, state: State) -> list[Action]:
        if not isinstance(event, MarkEvent) or event.instrument_id not in self.instrument_ids:
            return []

        prev = self._prev_mark.get(event.instrument_id)
        self._prev_mark[event.instrument_id] = event.mark_price
        if not prev:
            return []

        move_bps = (event.mark_price - prev) / prev * 10_000
        if abs(move_bps) < self.threshold_bps:
            return []

        side = Side.BUY if move_bps > 0 else Side.SELL
        return [PlaceOrder(instrument_id=event.instrument_id, side=side, qty=self.order_qty)]
