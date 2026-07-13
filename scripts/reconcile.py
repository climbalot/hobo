"""Admin reconciliation: compare engine positions to the exchange, and (only with
--apply) patch the log to adopt the exchange truth. Run with the engine STOPPED -
the log is single-writer.

    python scripts/reconcile.py                          # show the gap, change nothing
    python scripts/reconcile.py --apply --book scalper   # patch: adopt exchange truth

--apply appends a reconciliation FILL per gapped instrument (attributed to the book,
at the current mark), auditable and deterministic on replay.
"""

from __future__ import annotations

import argparse
import time

from hobo.adapters.factory import build_adapter
from hobo.config import Config
from hobo.log.events import Fill, ReconciliationWarning
from hobo.log.writer import LogWriter
from hobo.builder import _recover

TOLERANCE = 1e-6


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reconcile engine positions against the exchange; optionally patch the log.")
    p.add_argument("--apply", action="store_true", help="write the reconciliation patch (default: read-only)")
    p.add_argument("--book", help="book to attribute the adopted positions to (required with --apply)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    config = Config.load()
    adapter = build_adapter(config.exchange)
    writer = LogWriter(config.durability.event_log_path)  # opens/creates the log (single-writer: engine must be stopped)
    try:
        state = _recover(config, adapter).state
        account = adapter.account()
        if account is None:
            print("no authenticated exchange account (paper / no creds) - nothing to reconcile.")
            return 0
        exchange_positions = account.positions()

        engine: dict[str, float] = {}
        for book in state.desk.books.values():
            for iid, pos in book.positions.items():
                engine[iid] = engine.get(iid, 0.0) + pos.qty

        print(f"{'instrument':<20} {'engine':>12} {'exchange':>12} {'diff':>12}")
        gaps: dict[str, float] = {}
        for iid in sorted(set(engine) | set(exchange_positions)):
            eng, exc = engine.get(iid, 0.0), exchange_positions.get(iid, 0.0)
            diff = exc - eng
            flag = "" if abs(diff) <= TOLERANCE else "  <- MISMATCH"
            print(f"{iid:<20} {eng:>12.4f} {exc:>12.4f} {diff:>12.4f}{flag}")
            if abs(diff) > TOLERANCE:
                gaps[iid] = diff

        if not gaps:
            print("\nin sync - nothing to patch.")
            return 0
        if not args.apply:
            print(f"\n{len(gaps)} mismatch(es). Re-run with --apply --book <book> to adopt the exchange positions.")
            return 1

        if not args.book or args.book not in state.desk.books:
            print(f"\n--apply requires --book from {sorted(state.desk.books)}")
            return 2

        for iid, diff in gaps.items():
            side = "BUY" if diff > 0 else "SELL"
            mark = state.mark(iid)
            order_id = f"RECON-{iid}-{time.time_ns()}"
            # Audit the intent, then the position-adopting fill (deterministic on replay).
            warning = ReconciliationWarning("POSITION_PATCH", {"book_id": args.book, "instrument_id": iid, "adopted_diff": diff})
            writer.append(warning.event_type, warning.to_dict(), time.time_ns())
            fill = Fill(order_id, args.book, iid, side, abs(diff), mark, 0.0)
            writer.append(fill.event_type, fill.to_dict(), time.time_ns())
            print(f"patched {iid}: {side} {abs(diff):.4f} @ {mark} -> book {args.book!r}")
        writer.commit()
        print("\ndone. Restart the engine; replayed positions now match the exchange.")
        return 0
    finally:
        writer.close()
        adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
