"""Portfolio model: position -> book (a strategy's unit) -> desk (the portfolio).
Nothing here mutates itself - only hobo.risk.fold.apply_event does.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum


class Limit(str, Enum):
    NOTIONAL = "NOTIONAL"  # cap on aggregate gross notional
    DRAWDOWN = "DRAWDOWN"  # cap on peak-to-trough PnL
    POSITION = "POSITION"  # per-instrument position cap (books only)


def _limits_to_dict(limits: dict[Limit, float]) -> dict[str, float]:
    return {k.value: v for k, v in limits.items()}


def _limits_from_dict(d: dict[str, float]) -> dict[Limit, float]:
    return {Limit(k): v for k, v in d.items()}


@dataclass
class Position:
    instrument_id: str
    qty: float = 0.0  # signed: +long, -short, in contracts
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0  # from closing fills only (gross of fees), USDT
    funding_pnl: float = 0.0  # cumulative funding payments (USDT); signed
    fees_paid: float = 0.0  # cumulative trade fees (USDT); positive = cost

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(**d)


@dataclass
class Book:
    """A strategy's trading unit: its risk limits and a position per instrument it
    trades. Notional/drawdown limits apply to the book's aggregate; the position
    limit applies per instrument (contracts don't aggregate across instruments)."""

    book_id: str
    limits: dict[Limit, float]  # NOTIONAL + DRAWDOWN + POSITION
    positions: dict[str, Position] = field(default_factory=dict)
    high_water_mark_usdt: float = 0.0
    kill_switch: bool = False

    def position(self, instrument_id: str) -> Position:
        """The book's position in an instrument, or a fresh flat one (not stored -
        the fold uses `get_or_create` to mutate; queries stay read-only)."""
        return self.positions.get(instrument_id) or Position(instrument_id=instrument_id)

    def get_or_create(self, instrument_id: str) -> Position:
        pos = self.positions.get(instrument_id)
        if pos is None:
            pos = Position(instrument_id=instrument_id)
            self.positions[instrument_id] = pos
        return pos

    def to_dict(self) -> dict:
        return {
            "book_id": self.book_id,
            "limits": _limits_to_dict(self.limits),
            "positions": {iid: p.to_dict() for iid, p in self.positions.items()},
            "high_water_mark_usdt": self.high_water_mark_usdt,
            "kill_switch": self.kill_switch,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Book":
        return cls(
            book_id=d["book_id"],
            limits=_limits_from_dict(d["limits"]),
            positions={iid: Position.from_dict(pd) for iid, pd in d["positions"].items()},
            high_water_mark_usdt=d["high_water_mark_usdt"],
            kill_switch=d["kill_switch"],
        )


@dataclass
class Desk:
    """The portfolio: aggregate limits over the books (strategies) it holds."""

    desk_id: str
    limits: dict[Limit, float]  # NOTIONAL + DRAWDOWN
    books: dict[str, Book] = field(default_factory=dict)
    high_water_mark_usdt: float = 0.0
    kill_switch: bool = False
    kill_switch_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "desk_id": self.desk_id,
            "limits": _limits_to_dict(self.limits),
            "books": {book_id: b.to_dict() for book_id, b in self.books.items()},
            "high_water_mark_usdt": self.high_water_mark_usdt,
            "kill_switch": self.kill_switch,
            "kill_switch_reason": self.kill_switch_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Desk":
        return cls(
            desk_id=d["desk_id"],
            limits=_limits_from_dict(d["limits"]),
            books={book_id: Book.from_dict(bd) for book_id, bd in d["books"].items()},
            high_water_mark_usdt=d["high_water_mark_usdt"],
            kill_switch=d["kill_switch"],
            kill_switch_reason=d["kill_switch_reason"],
        )
