"""Wraps an execution client, re-emitting its fill/order-update callbacks as
FillEvent / OrderUpdateEvent. Fills go through `on_fill` (the FillIngestor's
admission gate); order updates go straight to `on_order_update` (the bus).
"""

from __future__ import annotations

from collections.abc import Callable

from hobo.core.events import FillEvent, OrderUpdateEvent
from hobo.execution.base import ExecutionClient, FillData, OrderUpdate
from hobo.risk.model import Order


class OrderManager:
    def __init__(self, client: ExecutionClient) -> None:
        self.client = client
        self.on_fill: Callable[[FillEvent], object] | None = None
        self.on_order_update: Callable[[OrderUpdateEvent], object] | None = None
        client.on_fill = self._handle_fill
        client.on_order_update = self._handle_order_update

    def submit(self, order: Order, mark_price: float) -> None:
        self.client.submit(order, mark_price)

    def cancel(self, order_id: str) -> None:
        self.client.cancel(order_id)

    def _handle_fill(self, fill: FillData) -> None:
        if self.on_fill is not None:
            self.on_fill(
                FillEvent(
                    order_id=fill.order_id,
                    book_id=fill.book_id,
                    instrument_id=fill.instrument_id,
                    side=fill.side,
                    qty=fill.qty,
                    fill_price=fill.fill_price,
                    fee=fill.fee,
                    trade_id=fill.trade_id,
                )
            )

    def _handle_order_update(self, update: OrderUpdate) -> None:
        if self.on_order_update is not None:
            self.on_order_update(
                OrderUpdateEvent(
                    order_id=update.order_id,
                    book_id=update.book_id,
                    state=update.state,
                    reason=update.reason,
                )
            )
