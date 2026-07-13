"""Shared fixtures and helpers for the test suite, aligned to the final architecture.

The risk model is now dict-based: instruments are `dict[str, Instrument]`, limits are
`dict[Limit, float]`, `apply_event` lives in `hobo.risk.fold`, and log payloads are the
`hobo.log.events` dataclasses (there are no `ev.mark_update(...)` factory helpers in the
code anymore - the small builders below stand in for them).
"""

from __future__ import annotations

import pytest

from hobo.log.events import (
    Envelope,
    Fill,
    FundingUpdate,
    GateDecisionLog,
    KillSwitch,
    LimitChange,
    MarkUpdate,
    OrderRequest,
    ReconciliationWarning,
)
from hobo.risk.model import Instrument, Limit
from hobo.risk.state import State

INSTRUMENT_ID = "BTC-USDT-SWAP"


# --- model builders ---


def make_instrument(instrument_id: str = INSTRUMENT_ID, **overrides) -> Instrument:
    fields = dict(
        instrument_id=instrument_id,
        ct_val=0.01,
        ct_val_ccy="BTC",
        ct_mult=1.0,
        tick_sz=0.1,
        lot_sz=1.0,
        min_sz=1.0,
        max_leverage=10.0,
        maintenance_margin_rate=0.005,
        settle_ccy="USDT",
    )
    fields.update(overrides)
    return Instrument(**fields)


def desk_limits(notional: float = 700_000, drawdown: float = 50_000) -> dict[Limit, float]:
    return {Limit.NOTIONAL: notional, Limit.DRAWDOWN: drawdown}


def book_limits(position: float = 10, notional: float = 500_000, drawdown: float = 25_000) -> dict[Limit, float]:
    return {Limit.POSITION: position, Limit.NOTIONAL: notional, Limit.DRAWDOWN: drawdown}


def make_state(
    instrument: Instrument | None = None,
    *,
    desk: dict[Limit, float] | None = None,
    book_a: dict[Limit, float] | None = None,
    book_b: dict[Limit, float] | None = None,
    desk_id: str = "desk-1",
) -> State:
    instrument = instrument or make_instrument()
    return State.initial(
        instruments={instrument.instrument_id: instrument},
        desk_id=desk_id,
        desk_limits=desk if desk is not None else desk_limits(),
        book_specs=[
            ("A", book_a if book_a is not None else book_limits()),
            ("B", book_b if book_b is not None else book_limits()),
        ],
    )


# --- log payload builders (stand-ins for the removed ev.* factory helpers) ---


def envelope(seq: int, event_type: str, payload: dict, ts_ns: int | None = None) -> Envelope:
    return Envelope(seq=seq, ts_ns=ts_ns if ts_ns is not None else seq, event_type=event_type, payload=payload)


def mark_update(instrument_id: str, mark_price: float, ts_exch_ns: int) -> dict:
    return MarkUpdate(instrument_id, mark_price, ts_exch_ns).to_dict()


def fill(order_id, book_id, instrument_id, side, qty, fill_price, fee=0.0, trade_id="") -> dict:
    return Fill(order_id, book_id, instrument_id, side, qty, fill_price, fee, trade_id).to_dict()


def funding_update(instrument_id: str, funding_rate: float, funding_time_ns: int) -> dict:
    return FundingUpdate(instrument_id, funding_rate, funding_time_ns).to_dict()


def limit_change(scope, field, old, new, book_id=None) -> dict:
    return LimitChange(scope, field, old, new, book_id).to_dict()


def kill_switch(scope, id, enabled, reason="") -> dict:
    return KillSwitch(scope, id, enabled, reason).to_dict()


def order_request(order_id, book_id, instrument_id, side, qty, order_type="MARKET") -> dict:
    return OrderRequest(order_id, book_id, instrument_id, side, qty, order_type).to_dict()


def gate_decision(order_id, book_id, approved, reason, detail=None) -> dict:
    return GateDecisionLog(order_id, book_id, approved, reason, detail or {}).to_dict()


def reconciliation_warning(kind, detail) -> dict:
    return ReconciliationWarning(kind, detail).to_dict()


# --- fixtures ---


@pytest.fixture
def instrument() -> Instrument:
    return make_instrument()


@pytest.fixture
def instruments(instrument) -> dict[str, Instrument]:
    return {instrument.instrument_id: instrument}


@pytest.fixture
def fresh_state(instrument) -> State:
    return make_state(instrument)
