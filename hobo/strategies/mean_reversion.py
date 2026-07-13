"""Inventory-mean-reversion toy strategy (multi-instrument). Tracks per-instrument
inventory from its own fills; on each mark, if inventory has drifted past a
threshold from target, emits a corrective order back toward target.
"""

from __future__ import annotations

from collections.abc import Iterable

from hobo.core.events import Action, Event, FillEvent, MarkEvent, PlaceOrder
from hobo.risk.model import Side
from hobo.risk.state import State

DEFAULT_TARGET_QTY = 0.0
DEFAULT_DRIFT_THRESHOLD = 2.0
DEFAULT_ORDER_QTY = 1.0


class MeanReversionStrategy:
    def __init__(
        self,
        instrument_ids: Iterable[str],
        target_qty: float = DEFAULT_TARGET_QTY,
        drift_threshold: float = DEFAULT_DRIFT_THRESHOLD,
        order_qty: float = DEFAULT_ORDER_QTY,
    ) -> None:
        self.instrument_ids = set(instrument_ids)
        self.target_qty = target_qty
        self.drift_threshold = drift_threshold
        self.order_qty = order_qty
        self._position: dict[str, float] = {}

    def on_event(self, event: Event, state: State) -> list[Action]:
        if isinstance(event, FillEvent):
            signed = event.qty if event.side == Side.BUY.value else -event.qty
            self._position[event.instrument_id] = self._position.get(event.instrument_id, 0.0) + signed
            return []

        if not isinstance(event, MarkEvent) or event.instrument_id not in self.instrument_ids:
            return []

        drift = self._position.get(event.instrument_id, 0.0) - self.target_qty
        if abs(drift) < self.drift_threshold:
            return []

        side = Side.SELL if drift > 0 else Side.BUY
        return [PlaceOrder(instrument_id=event.instrument_id, side=side, qty=self.order_qty)]
