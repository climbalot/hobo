"""Crash-recovery flow: load snapshot -> replay log tail -> reconcile -> resume.
Startup reconciliation covers instrument reference-data drift and mark-price
staleness, each degrading to a logged ReconciliationWarning plus a corrective seed -
never a silent guess. The event log remains the sole source of truth for positions.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Callable

from hobo.adapters.base import MarketData, ReferenceData
from hobo.log import reader
from hobo.log import snapshot as snap
from hobo.log.events import ReconciliationWarning
from hobo.risk.model import Book, ContractSpec, Instrument, Limit
from hobo.risk.fold import apply_event
from hobo.risk.state import State

MARK_STALENESS_THRESHOLD_NS = 5_000_000_000  # 5s - matches the feed staleness watchdog threshold


@dataclass
class RecoveryResult:
    state: State
    warnings: list[ReconciliationWarning] = field(default_factory=list)
    recovered_from_snapshot: bool = False
    replayed_event_count: int = 0


def recover_state(
    log_path: str,
    snapshot_dir: str,
    fresh_state_factory: Callable[[], State],
    reference_client: ReferenceData | None = None,
    market_client: MarketData | None = None,
    now_ns: int | None = None,
    book_specs: list[tuple[str, dict[Limit, float]]] | None = None,
) -> RecoveryResult:
    snapshot_state = snap.load_latest_state(snapshot_dir)
    recovered_from_snapshot = snapshot_state is not None
    state = snapshot_state if snapshot_state is not None else fresh_state_factory()

    mm = reader.open_readonly(log_path)
    replayed = 0
    for envelope in reader.replay(mm):
        if envelope.seq > state.last_seq:
            apply_event(state, envelope)
            replayed += 1

    warnings: list[ReconciliationWarning] = []
    if book_specs is not None:
        warnings.extend(_reconcile_books(state, book_specs))
    if reference_client is not None:
        warnings.extend(_reconcile_instrument(state, reference_client))
    if market_client is not None:
        warnings.extend(_reconcile_mark_freshness(state, market_client, now_ns))

    return RecoveryResult(
        state=state,
        warnings=warnings,
        recovered_from_snapshot=recovered_from_snapshot,
        replayed_event_count=replayed,
    )


def _reconcile_books(state: State, book_specs: list[tuple[str, dict[Limit, float]]]) -> list[ReconciliationWarning]:
    """Reconcile the recovered book roster against the configured seed roster.

    The roster is config, not event-sourced state: a book added to the seed is
    onboarded fresh (positions/PnL stay per-book event-derived; existing books'
    replayed limits are left untouched). A book dropped from the seed is kept but
    flagged - never silently discarded, since it may hold an open position.
    """
    warnings: list[ReconciliationWarning] = []
    seed_ids = {book_id for book_id, _ in book_specs}

    for book_id, limits in book_specs:
        if book_id not in state.desk.books:
            state.desk.books[book_id] = Book(book_id=book_id, limits=limits)
            warnings.append(ReconciliationWarning("BOOK_ADDED", {"book_id": book_id}))

    for book_id, book in state.desk.books.items():
        if book_id not in seed_ids:
            open_qty = {iid: p.qty for iid, p in book.positions.items() if p.qty != 0}
            warnings.append(ReconciliationWarning("BOOK_NOT_IN_SEED", {"book_id": book_id, "open_positions": open_qty}))

    return warnings


def _spec_of(instrument: Instrument) -> ContractSpec:
    return ContractSpec(**{f.name: getattr(instrument, f.name) for f in dataclasses.fields(ContractSpec)})


def _reconcile_instrument(state: State, client: ReferenceData) -> list[ReconciliationWarning]:
    warnings: list[ReconciliationWarning] = []
    for instrument_id, instrument in state.instruments.items():
        fresh = client.fetch_contract_spec(instrument_id)
        current = _spec_of(instrument)
        if fresh == current:
            continue
        diff = {
            f.name: (getattr(current, f.name), getattr(fresh, f.name))
            for f in dataclasses.fields(ContractSpec)
            if getattr(current, f.name) != getattr(fresh, f.name)
        }
        state.instruments[instrument_id] = Instrument.from_spec(fresh, instrument.maintenance_margin_rate)
        warnings.append(ReconciliationWarning("INSTRUMENT_SPEC_DRIFT", {"instrument_id": instrument_id, "diff": diff}))
    return warnings


def _reconcile_mark_freshness(state: State, client: MarketData, now_ns: int | None) -> list[ReconciliationWarning]:
    now = now_ns if now_ns is not None else time.time_ns()
    warnings: list[ReconciliationWarning] = []
    for instrument_id in state.instruments:
        last_ts = state.mark_ts_ns.get(instrument_id)
        age_ns = None if last_ts is None else now - last_ts
        if last_ts is not None and age_ns <= MARK_STALENESS_THRESHOLD_NS:
            continue
        price, ts_ns = client.fetch_mark(instrument_id)
        state.marks[instrument_id] = price
        state.mark_ts_ns[instrument_id] = ts_ns
        warnings.append(
            ReconciliationWarning("MARK_STALE", {"instrument_id": instrument_id, "age_ns": age_ns, "seeded_price": price})
        )
    return warnings
