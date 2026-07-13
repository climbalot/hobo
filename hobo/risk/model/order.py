"""Order model: side and the order itself. The gate's verdict lives in decision.py."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


@dataclass(frozen=True)
class Order:
    order_id: str
    book_id: str
    instrument_id: str
    side: Side
    qty: float
    ts_ns: int
    order_type: str = "MARKET"

    @property
    def signed_qty(self) -> float:
        return self.side.sign() * self.qty
