"""Recent-trades blotter for the dashboard: a bounded in-memory ring of the latest
fills (a FillEvent subscriber, not a source of truth), seeded from the log on
startup so it shows recent history immediately.
"""

from __future__ import annotations

import time
from collections import deque

from hobo.core.events import FillEvent
from hobo.log import reader
from hobo.log.events import EventType

DEFAULT_MAXLEN = 50


class TradeLog:
    def __init__(self, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._trades: deque[dict] = deque(maxlen=maxlen)

    def load_history(self, log_path: str) -> None:
        """Seed from the event log's FILL history. The deque bound keeps only the
        most recent `maxlen`, so a large log still yields a fixed-size window."""
        for envelope in reader.replay(reader.open_readonly(log_path)):
            if envelope.event_type == EventType.FILL:
                p = envelope.payload
                self._trades.append(
                    {
                        "ts_ms": envelope.ts_ns // 1_000_000,
                        "instrument_id": p["instrument_id"], "book_id": p["book_id"],
                        "side": p["side"],
                        "qty": p["qty"],
                        "price": p["fill_price"],
                        "fee": p.get("fee", 0.0),
                        "order_id": p["order_id"],
                    }
                )

    def record(self, event: FillEvent) -> None:
        self._trades.append(
            {
                "ts_ms": time.time_ns() // 1_000_000,
                "instrument_id": event.instrument_id, "book_id": event.book_id,
                "side": event.side,
                "qty": event.qty,
                "price": event.fill_price,
                "fee": event.fee,
                "order_id": event.order_id,
            }
        )

    def recent(self) -> list[dict]:
        return list(reversed(self._trades))  # newest first
