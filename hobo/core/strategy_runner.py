"""StrategyRunner: bus subscriber that owns the strategy<->book pairing. Delivers
each event to each strategy (routing book-scoped fills/order-updates only to the
owning book) and attaches the book when handing intents to the OrderGateway, so
strategies stay book-agnostic.
"""

from __future__ import annotations

from hobo.core.events import CancelOrder, Event, FillEvent, OrderUpdateEvent, PlaceOrder
from hobo.core.gateway import OrderGateway
from hobo.core.state_store import StateStore
from hobo.strategies.base import Strategy


class StrategyRunner:
    def __init__(self, strategies: list[tuple[str, Strategy]], store: StateStore, gateway: OrderGateway) -> None:
        self._strategies = strategies
        self._store = store
        self._gateway = gateway

    def handle(self, event: Event) -> None:
        for book_id, strategy in self._strategies:
            if _belongs_to_other_book(event, book_id):
                continue
            for action in strategy.on_event(event, self._store.state):
                if isinstance(action, PlaceOrder):
                    self._gateway.place(book_id, action.instrument_id, action.side, action.qty, action.order_type)
                elif isinstance(action, CancelOrder):
                    self._gateway.cancel(action.order_id)


def _belongs_to_other_book(event: Event, book_id: str) -> bool:
    """A book's own fills/order-updates go only to that book's strategy."""
    return isinstance(event, (FillEvent, OrderUpdateEvent)) and event.book_id != book_id
