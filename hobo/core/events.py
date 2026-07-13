"""In-memory runtime events and strategy actions for the event-driven loop.

These are the objects the bus (hobo/bus.py) routes: inbound Events
(market data + execution feedback) and the Actions strategies emit in
response. Distinct from the durable log payloads in hobo/log/events.py -
those are the on-disk audit record; these are transient dispatch objects.
"""

from __future__ import annotations

from dataclasses import dataclass

from hobo.risk.model import Side


@dataclass(frozen=True)
class MarkEvent:
    instrument_id: str
    mark_price: float
    ts_ns: int


@dataclass(frozen=True)
class FundingEvent:
    instrument_id: str
    funding_rate: float
    funding_time_ns: int
    ts_ns: int


@dataclass(frozen=True)
class FillEvent:
    order_id: str
    book_id: str
    instrument_id: str
    side: str  # "BUY" | "SELL"
    qty: float
    fill_price: float
    fee: float = 0.0  # trade fee for this fill (USDT); positive = cost paid
    trade_id: str = ""  # exchange execution id, for reconciliation dedup


@dataclass(frozen=True)
class OrderUpdateEvent:
    order_id: str
    book_id: str
    state: str  # "ACCEPTED" | "REJECTED" | "CANCELED"
    reason: str = ""


@dataclass(frozen=True)
class PlaceOrder:
    """Book-agnostic order intent. The StrategyRunner attaches the book and the
    OrderGateway mints the order id - a strategy expresses instrument + side + size."""

    instrument_id: str
    side: Side
    qty: float
    order_type: str = "MARKET"


@dataclass(frozen=True)
class CancelOrder:
    order_id: str


Event = MarkEvent | FundingEvent | FillEvent | OrderUpdateEvent
Action = PlaceOrder | CancelOrder
