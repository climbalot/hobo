"""The pre-trade preview: an order's hypothetical post-fill effect, computed once so
the checks (checks.py) stay pure lookups. `build_order_context` simulates the fill
and folds it into the book- and desk-level aggregates every scope a check reads.
"""

from __future__ import annotations

from dataclasses import dataclass

from hobo.risk.math import notional, simulate_fill, unrealized_pnl
from hobo.risk.model import Order
from hobo.risk.state import State


@dataclass(frozen=True)
class OrderContext:
    """An order plus its hypothetical post-fill values, computed once and shared by
    every check. `new_qty` is the book's position in the *order's instrument* after
    the fill; the notional/PnL fields are the book- and desk-level aggregates after it."""

    order: Order
    mark: float
    new_qty: float
    new_book_notional: float
    hypothetical_book_pnl: float
    new_desk_gross_notional: float
    hypothetical_desk_pnl: float

    @property
    def book_id(self) -> str:
        return self.order.book_id


def build_order_context(state: State, order: Order) -> OrderContext:
    book = state.desk.books[order.book_id]
    instrument = state.instruments[order.instrument_id]
    mark = state.mark(order.instrument_id)
    pos = book.position(order.instrument_id)  # read-only get: fresh flat if none held yet

    new_qty, new_entry_price, realized_delta = simulate_fill(
        qty_before=pos.qty,
        entry_before=pos.avg_entry_price,
        delta=order.signed_qty,
        fill_price=mark,
        instrument=instrument,
    )
    # Swap this instrument's old->new contribution into the book aggregates, then the
    # book's into the desk aggregates - both scopes reflect the fill without rescanning.
    fees_and_funding = pos.funding_pnl - pos.fees_paid  # unchanged by a new order (its own fee isn't previewed)
    old_pnl = pos.realized_pnl + fees_and_funding + unrealized_pnl(pos.qty, pos.avg_entry_price, mark, instrument)
    new_pnl = pos.realized_pnl + realized_delta + fees_and_funding + unrealized_pnl(new_qty, new_entry_price, mark, instrument)
    old_notional = notional(pos.qty, mark, instrument)
    new_notional = notional(new_qty, mark, instrument)

    new_book_notional = state.book_notional(book) - old_notional + new_notional
    hypothetical_book_pnl = state.book_total_pnl(book) - old_pnl + new_pnl

    return OrderContext(
        order=order,
        mark=mark,
        new_qty=new_qty,
        new_book_notional=new_book_notional,
        hypothetical_book_pnl=hypothetical_book_pnl,
        new_desk_gross_notional=state.desk_gross_notional() - state.book_notional(book) + new_book_notional,
        hypothetical_desk_pnl=state.desk_total_pnl() - state.book_total_pnl(book) + hypothetical_book_pnl,
    )
