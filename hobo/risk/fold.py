"""The event fold: `apply_event` is the single place State mutates. It is plain
synchronous - never `await`s mid-computation - so within one event loop there is no
torn-read hazard despite State being shared by the gate, log writer, and cold path.
"""

from __future__ import annotations

from collections.abc import Callable

from hobo.log.events import Envelope, EventType
from hobo.risk.math import simulate_fill
from hobo.risk.model import Limit
from hobo.risk.state import State


def apply_event(state: State, envelope: Envelope) -> State:
    handler = _FOLD_HANDLERS.get(envelope.event_type)
    if handler is None:
        raise ValueError(f"unknown event_type: {envelope.event_type!r}")
    handler(state, envelope.payload)
    state.last_seq = envelope.seq
    return state


def _noop(state: State, p: dict) -> None:
    # Audit-only events (ORDER_REQUEST / GATE_DECISION / RECONCILIATION_WARNING) don't mutate state on fold.
    pass


def _apply_mark(state: State, p: dict) -> None:
    state.marks[p["instrument_id"]] = p["mark_price"]
    state.mark_ts_ns[p["instrument_id"]] = p["ts_exch_ns"]
    _update_high_water_marks(state)


def _apply_fill(state: State, p: dict) -> None:
    instrument_id = p["instrument_id"]
    pos = state.desk.books[p["book_id"]].get_or_create(instrument_id)
    delta = p["qty"] if p["side"] == "BUY" else -p["qty"]

    new_qty, new_entry_price, realized_delta = simulate_fill(
        qty_before=pos.qty,
        entry_before=pos.avg_entry_price,
        delta=delta,
        fill_price=p["fill_price"],
        instrument=state.instruments[instrument_id],
    )
    pos.qty = new_qty
    pos.avg_entry_price = new_entry_price
    pos.realized_pnl += realized_delta
    pos.fees_paid += p.get("fee", 0.0)  # get(): old logs predate the fee field
    _update_high_water_marks(state)


def _apply_funding(state: State, p: dict) -> None:
    instrument_id = p["instrument_id"]
    funding_rate = p["funding_rate"]
    state.funding_rates[instrument_id] = funding_rate
    mark = state.mark(instrument_id)
    multiplier = state.instruments[instrument_id].multiplier

    for book in state.desk.books.values():
        pos = book.positions.get(instrument_id)
        if pos is None or pos.qty == 0:
            continue
        # Convention: positive funding_rate means longs pay shorts.
        pos.funding_pnl += -pos.qty * multiplier * mark * funding_rate
    _update_high_water_marks(state)


def _apply_limit_change(state: State, p: dict) -> None:
    if p["scope"] == "BOOK":
        limits = state.desk.books[p["book_id"]].limits
    elif p["scope"] == "DESK":
        limits = state.desk.limits
    else:
        raise ValueError(f"unknown limit_change scope: {p['scope']!r}")
    limits[Limit(p["field"])] = p["new"]  # `field` is a Limit name, e.g. "NOTIONAL"


def _apply_kill_switch(state: State, p: dict) -> None:
    if p["scope"] == "BOOK":
        state.desk.books[p["id"]].kill_switch = p["enabled"]
    elif p["scope"] == "DESK":
        state.desk.kill_switch = p["enabled"]
        state.desk.kill_switch_reason = p.get("reason", "")
    else:
        raise ValueError(f"unknown kill_switch scope: {p['scope']!r}")


def _update_high_water_marks(state: State) -> None:
    for book in state.desk.books.values():
        total = state.book_total_pnl(book)
        if total > book.high_water_mark_usdt:
            book.high_water_mark_usdt = total
    desk_total = state.desk_total_pnl()
    if desk_total > state.desk.high_water_mark_usdt:
        state.desk.high_water_mark_usdt = desk_total


# event_type -> fold handler(state, payload). apply_event dispatches through this.
_FOLD_HANDLERS: dict[str, Callable[[State, dict], None]] = {
    EventType.MARK_UPDATE: _apply_mark,
    EventType.FUNDING_UPDATE: _apply_funding,
    EventType.FILL: _apply_fill,
    EventType.LIMIT_CHANGE: _apply_limit_change,
    EventType.KILL_SWITCH: _apply_kill_switch,
    EventType.ORDER_REQUEST: _noop,
    EventType.GATE_DECISION: _noop,
    EventType.RECONCILIATION_WARNING: _noop,
}
