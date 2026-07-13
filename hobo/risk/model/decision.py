"""The pre-trade gate's verdict: why an order was rejected, and the decision it
produces. Output of the risk gate (hobo/risk/gate), separate from the order model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RejectReason(str, Enum):
    NONE = "NONE"
    KILL_SWITCH = "KILL_SWITCH"
    FAT_FINGER = "FAT_FINGER"
    BOOK_POSITION_LIMIT = "BOOK_POSITION_LIMIT"
    BOOK_NOTIONAL_LIMIT = "BOOK_NOTIONAL_LIMIT"
    BOOK_DRAWDOWN_LIMIT = "BOOK_DRAWDOWN_LIMIT"
    DESK_GROSS_NOTIONAL_LIMIT = "DESK_GROSS_NOTIONAL_LIMIT"
    DESK_DRAWDOWN_LIMIT = "DESK_DRAWDOWN_LIMIT"


@dataclass(frozen=True)
class GateDecision:
    order_id: str
    approved: bool
    reason: RejectReason
    detail: dict = field(default_factory=dict)
