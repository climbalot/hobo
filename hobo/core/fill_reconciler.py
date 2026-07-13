"""REST backstop that adopts WS-dropped fills through the shared FillIngestor
(dedup by trade_id). A continuing engine baselines just before the log's last fill
so a restart backfills downtime-gap fills; a fresh engine baselines at now, staying
forward-only so it never resurrects pre-existing history.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from hobo.adapters.base import AccountData
from hobo.core.events import FillEvent
from hobo.core.fill_ingestor import FillIngestor
from hobo.risk.state import State

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECS = 20.0
MIN_FILL_AGE_NS = 10_000_000_000  # 10s; let the (primary) WS deliver fresh fills first
MAX_PAGES = 10  # burst cap: page back only to the baseline
BASELINE_SLACK_NS = 60_000_000_000  # 60s; back off the log's last-fill time so gap fills land after it


class FillReconciler:
    def __init__(
        self,
        account: AccountData,
        ingestor: FillIngestor,
        state_provider: Callable[[], State],
        interval_secs: float = DEFAULT_INTERVAL_SECS,
    ) -> None:
        self._account = account
        self._ingestor = ingestor
        self._state_provider = state_provider
        self._interval = interval_secs
        self._baseline_ns = 0  # set at run() start

    async def run(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        last_fill_ns = self._ingestor.last_fill_ns()
        # Baseline before the log's last fill (backfill downtime gaps); fresh engine baselines at now.
        self._baseline_ns = last_fill_ns - BASELINE_SLACK_NS if last_fill_ns else time.time_ns()
        while not stop.is_set():
            try:
                # Page in a thread against a frozen seen-snapshot; submit on the loop (fold/log single-threaded).
                seen = self._ingestor.seen_copy()
                new_fills = await loop.run_in_executor(None, self._fetch_new, seen)
                self._submit(new_fills)
            except Exception:
                logger.warning("fill reconcile poll failed", exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    def _fetch_new(self, seen: set) -> list:
        """Page from newest toward the baseline, collecting not-yet-ingested fills after it."""
        collected: list = []
        after: str | None = None
        for _ in range(MAX_PAGES):
            page = self._account.fills(after)
            if not page:
                break
            collected.extend(f for f in page if f.ts_ns >= self._baseline_ns and f.trade_id and f.trade_id not in seen)
            if any(f.ts_ns < self._baseline_ns for f in page):
                break  # reached fills older than the baseline - stop
            after = page[-1].cursor
            if not after:
                break
        return collected

    def _submit(self, fills: list) -> None:
        books = self._state_provider().desk.books
        now_ns = time.time_ns()
        adopted = 0
        for f in reversed(fills):  # oldest-first so avg-entry / realized build in order
            if now_ns - f.ts_ns < MIN_FILL_AGE_NS:
                continue  # too fresh - let the WS deliver it first
            book_id = _attribute(f.client_order_id, books)
            if book_id is None:
                continue  # external / unattributable -> manual reconcile.py territory
            adopted += self._ingestor.submit(
                FillEvent(
                    order_id=f.client_order_id or f.trade_id,
                    book_id=book_id,
                    instrument_id=f.instrument_id,
                    side=f.side,
                    qty=f.qty,
                    fill_price=f.price,
                    fee=f.fee,
                    trade_id=f.trade_id,
                )
            )  # ingestor dedups: only genuinely-missed fills count
        if adopted:
            logger.info("fill reconcile: adopted %d WS-missed fill(s)", adopted)


def _attribute(client_order_id: str, books) -> str | None:
    """Our clOrdId is `<book_id><seq digits>`; match the longest book-id prefix."""
    for book_id in sorted(books, key=len, reverse=True):
        rest = client_order_id[len(book_id):]
        if client_order_id.startswith(book_id) and rest and rest.isdigit():
            return book_id
    return None
