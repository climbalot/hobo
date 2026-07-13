"""Continuous exchange reconciliation for the dashboard: periodically compares the
engine's event-sourced positions against the exchange's account truth, surfacing
divergence. Polls on its own daemon thread so signed REST never touches the hot path.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from hobo.adapters.base import AccountData
from hobo.risk.state import State

DEFAULT_INTERVAL_SECS = 15.0
DEFAULT_TOLERANCE = 1e-9


class ExchangeReconciler:
    def __init__(
        self,
        account: AccountData,
        state_provider: Callable[[], State],
        interval_secs: float = DEFAULT_INTERVAL_SECS,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> None:
        self._account = account
        self._state_provider = state_provider
        self._interval = interval_secs
        self._tolerance = tolerance
        self._latest: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def latest(self) -> dict | None:
        return self._latest

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._poll()
            self._stop.wait(self._interval)

    def _poll(self) -> None:
        try:
            exchange_positions = self._account.positions()
            balance = self._account.balance()
        except Exception as exc:  # network/auth hiccup shouldn't kill the loop
            self._latest = {"ok": False, "error": str(exc)[:200]}
            return

        engine: dict[str, float] = {}
        for book in self._state_provider().desk.books.values():
            for iid, pos in book.positions.items():
                engine[iid] = engine.get(iid, 0.0) + pos.qty

        instruments = {}
        for iid in set(engine) | set(exchange_positions):
            eng_qty, exc_qty = engine.get(iid, 0.0), exchange_positions.get(iid, 0.0)
            diff = eng_qty - exc_qty
            instruments[iid] = {
                "engine_qty": eng_qty,
                "exchange_qty": exc_qty,
                "diff": diff,
                "matched": abs(diff) <= self._tolerance,
            }
        self._latest = {
            "ok": True,
            "ts_ms": time.time_ns() // 1_000_000,  # poll time; positions/balance are separate reads
            "total_equity_usdt": balance.total_equity_usdt,
            "instruments": instruments,
        }
