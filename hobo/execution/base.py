"""Execution client abstraction: where already-approved orders go (the gate runs
before submit) and how fills return - via async callbacks (`on_fill`,
`on_order_update`), not return values. Implemented by the paper client and the
real exchange client.
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass

from hobo.risk.model import Order


@dataclass(frozen=True)
class FillData:
    order_id: str
    book_id: str
    instrument_id: str
    side: str  # "BUY" | "SELL"
    qty: float
    fill_price: float
    fee: float = 0.0  # trade fee for this fill (USDT); positive = cost paid
    trade_id: str = ""  # exchange execution id, for reconciliation dedup


@dataclass(frozen=True)
class OrderUpdate:
    order_id: str
    book_id: str
    state: str  # "ACCEPTED" | "REJECTED" | "CANCELED"
    reason: str = ""


class ExecutionClient(abc.ABC):
    def __init__(self) -> None:
        self.on_fill: Callable[[FillData], None] | None = None
        self.on_order_update: Callable[[OrderUpdate], None] | None = None

    @abc.abstractmethod
    def submit(self, order: Order, mark_price: float) -> None: ...

    @abc.abstractmethod
    def cancel(self, order_id: str) -> None: ...

    def _emit_fill(self, fill: FillData) -> None:
        if self.on_fill is not None:
            self.on_fill(fill)

    def _emit_order_update(self, update: OrderUpdate) -> None:
        if self.on_order_update is not None:
            self.on_order_update(update)
