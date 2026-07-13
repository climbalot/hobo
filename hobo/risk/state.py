"""The materialized risk state: the model (instruments, marks/funding, desk/book
positions) plus pure read-only queries over it (PnL, drawdown, notional). It never
mutates itself - the fold lives in hobo.risk.fold. A book holds a position per
instrument; book-level queries aggregate across them.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from hobo.risk.math import notional, unrealized_pnl
from hobo.risk.model import Book, Desk, Instrument, Limit


@dataclass
class State:
    instruments: dict[str, Instrument]
    desk: Desk
    marks: dict[str, float] = field(default_factory=dict)
    mark_ts_ns: dict[str, int] = field(default_factory=dict)
    funding_rates: dict[str, float] = field(default_factory=dict)
    last_seq: int = 0
    feed_stale: bool = False

    @classmethod
    def initial(
        cls,
        instruments: dict[str, Instrument],
        desk_id: str,
        desk_limits: dict[Limit, float],
        book_specs: list[tuple[str, dict[Limit, float]]],
    ) -> "State":
        desk = Desk(desk_id=desk_id, limits=desk_limits)
        for book_id, limits in book_specs:
            desk.books[book_id] = Book(book_id=book_id, limits=limits)
        return cls(instruments=instruments, desk=desk)

    def mark(self, instrument_id: str) -> float:
        return self.marks.get(instrument_id, 0.0)

    # --- per-book aggregates (across the book's instrument positions) ---

    def book_unrealized_pnl(self, book: Book) -> float:
        return sum(
            unrealized_pnl(p.qty, p.avg_entry_price, self.mark(iid), self.instruments[iid])
            for iid, p in book.positions.items()
        )

    def book_realized_pnl(self, book: Book) -> float:
        return sum(p.realized_pnl for p in book.positions.values())

    def book_funding_pnl(self, book: Book) -> float:
        return sum(p.funding_pnl for p in book.positions.values())

    def book_fees_paid(self, book: Book) -> float:
        return sum(p.fees_paid for p in book.positions.values())

    def book_notional(self, book: Book) -> float:
        return sum(notional(p.qty, self.mark(iid), self.instruments[iid]) for iid, p in book.positions.items())

    def book_total_pnl(self, book: Book) -> float:
        # Attribution: realized (closing fills) + funding - fees + live MTM.
        return (
            self.book_realized_pnl(book)
            + self.book_funding_pnl(book)
            - self.book_fees_paid(book)
            + self.book_unrealized_pnl(book)
        )

    def book_drawdown(self, book: Book) -> float:
        return max(0.0, book.high_water_mark_usdt - self.book_total_pnl(book))

    # --- desk aggregates ---

    def desk_total_pnl(self) -> float:
        return sum(self.book_total_pnl(b) for b in self.desk.books.values())

    def desk_gross_notional(self) -> float:
        return sum(self.book_notional(b) for b in self.desk.books.values())

    def desk_drawdown(self) -> float:
        return max(0.0, self.desk.high_water_mark_usdt - self.desk_total_pnl())

    def to_dict(self) -> dict:
        return {
            "instruments": {iid: dataclasses.asdict(instr) for iid, instr in self.instruments.items()},
            "desk": self.desk.to_dict(),
            "marks": dict(self.marks),
            "mark_ts_ns": dict(self.mark_ts_ns),
            "funding_rates": dict(self.funding_rates),
            "last_seq": self.last_seq,
            "feed_stale": self.feed_stale,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        return cls(
            instruments={iid: Instrument(**idict) for iid, idict in d["instruments"].items()},
            desk=Desk.from_dict(d["desk"]),
            marks=dict(d["marks"]),
            mark_ts_ns=dict(d.get("mark_ts_ns", {})),
            funding_rates=dict(d["funding_rates"]),
            last_seq=d["last_seq"],
            feed_stale=d["feed_stale"],
        )
