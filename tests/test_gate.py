from __future__ import annotations

import copy

from hobo.log.events import EventType
from hobo.risk.fold import apply_event
from hobo.risk.gate.engine import default_engine
from hobo.risk.math import notional
from hobo.risk.model import Limit, Order, RejectReason, Side

from conftest import INSTRUMENT_ID, book_limits, desk_limits, envelope, fill, kill_switch, make_state, mark_update


def gated_state(instrument, *, desk=None, book_a=None, book_b=None, mark=50_000.0):
    state = make_state(instrument, desk=desk, book_a=book_a, book_b=book_b)
    apply_event(state, envelope(1, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, mark, 1)))
    return state


def make_order(book_id, side, qty, instrument_id=INSTRUMENT_ID) -> Order:
    return Order(
        order_id=f"o-{book_id}-{side.value}-{qty}",
        book_id=book_id,
        instrument_id=instrument_id,
        side=side,
        qty=qty,
        ts_ns=1,
    )


def test_approve_within_all_limits(instrument):
    state = gated_state(instrument)
    decision = default_engine().check(state, make_order("A", Side.BUY, 5))
    assert decision.approved is True
    assert decision.reason == RejectReason.NONE


def test_approve_does_not_mutate_state(instrument):
    state = gated_state(instrument)
    before = copy.deepcopy(state.to_dict())
    default_engine().check(state, make_order("A", Side.BUY, 5))
    assert state.to_dict() == before


def test_fat_finger_rejects_oversized_order(instrument):
    # Default max order size is 1000; a huge single order trips the order-level guard first.
    state = gated_state(instrument, book_a=book_limits(position=100_000, notional=10**12))
    decision = default_engine().check(state, make_order("A", Side.BUY, 5000))
    assert decision.approved is False
    assert decision.reason == RejectReason.FAT_FINGER


def test_book_position_limit_rejects(instrument):
    state = gated_state(instrument)
    decision = default_engine().check(state, make_order("A", Side.BUY, 11))
    assert decision.approved is False
    assert decision.reason == RejectReason.BOOK_POSITION_LIMIT


def test_book_notional_limit_rejects(instrument):
    state = gated_state(
        instrument,
        desk=desk_limits(notional=10_000_000),
        book_a=book_limits(position=1000, notional=100_000),
        mark=50_000,
    )
    # 250 contracts * 0.01 * $50,000 = $125,000 > $100,000 book notional limit
    decision = default_engine().check(state, make_order("A", Side.BUY, 250))
    assert decision.approved is False
    assert decision.reason == RejectReason.BOOK_NOTIONAL_LIMIT


def test_book_drawdown_limit_rejects(instrument):
    state = gated_state(
        instrument,
        desk=desk_limits(notional=10_000_000, drawdown=1_000_000),
        book_a=book_limits(position=1000, notional=10_000_000, drawdown=40),
        book_b=book_limits(position=1000, notional=10_000_000, drawdown=1_000_000),
        mark=50_000,
    )
    # Long, rally sets a high-water mark, then mark falls back - unrealized drawdown from the peak.
    apply_event(state, envelope(2, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000)))
    apply_event(state, envelope(3, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 51_000, 3)))
    apply_event(state, envelope(4, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 50_500, 4)))

    decision = default_engine().check(state, make_order("A", Side.BUY, 1))
    assert decision.approved is False
    assert decision.reason == RejectReason.BOOK_DRAWDOWN_LIMIT


def test_kill_switch_desk_scope_rejects(instrument):
    state = gated_state(instrument)
    apply_event(state, envelope(2, EventType.KILL_SWITCH, kill_switch("DESK", "desk-1", True, "feed stale")))
    decision = default_engine().check(state, make_order("A", Side.BUY, 1))
    assert decision.approved is False
    assert decision.reason == RejectReason.KILL_SWITCH
    assert decision.detail["scope"] == "DESK"


def test_kill_switch_book_scope_rejects_only_that_book(instrument):
    state = gated_state(instrument)
    apply_event(state, envelope(2, EventType.KILL_SWITCH, kill_switch("BOOK", "A", True, "manual halt")))

    engine = default_engine()
    decision_a = engine.check(state, make_order("A", Side.BUY, 1))
    assert decision_a.approved is False
    assert decision_a.reason == RejectReason.KILL_SWITCH

    decision_b = engine.check(state, make_order("B", Side.BUY, 1))
    assert decision_b.approved is True


def test_book_ok_but_desk_aggregate_breach_is_rejected(instrument):
    """The load-bearing two-level gate: Book B's order is within B's own limits, but Book A
    already holds enough notional that adding B pushes the desk aggregate over its cap."""
    state = gated_state(
        instrument,
        desk=desk_limits(notional=700_000),
        book_a=book_limits(position=2000, notional=500_000),
        book_b=book_limits(position=2000, notional=500_000),
        mark=50_000,
    )
    # Book A: 900 contracts * 0.01 * $50,000 = $450,000 (within its own $500k limit).
    apply_event(state, envelope(2, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 900, 50_000)))

    engine = default_engine()
    order_b = make_order("B", Side.BUY, 900)  # B alone: $450,000 (< its $500k limit)

    # Against a lifted desk cap the same order is approved - proving the rejection is desk-only.
    isolated = gated_state(
        instrument,
        desk=desk_limits(notional=10_000_000),
        book_a=book_limits(position=2000, notional=500_000),
        book_b=book_limits(position=2000, notional=500_000),
        mark=50_000,
    )
    apply_event(isolated, envelope(2, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 900, 50_000)))
    assert engine.check(isolated, order_b).approved is True

    decision = engine.check(state, order_b)
    assert decision.approved is False
    assert decision.reason == RejectReason.DESK_GROSS_NOTIONAL_LIMIT

    # Confirm B's own notional stayed within its book limit (genuine desk-only rejection).
    assert notional(900, 50_000, instrument) <= state.desk.books["B"].limits[Limit.NOTIONAL]


def test_desk_drawdown_limit_rejects(instrument):
    state = gated_state(
        instrument,
        desk=desk_limits(notional=10_000_000, drawdown=40),
        book_a=book_limits(position=1000, notional=10_000_000, drawdown=1_000_000),
        book_b=book_limits(position=1000, notional=10_000_000, drawdown=1_000_000),
        mark=50_000,
    )
    apply_event(state, envelope(2, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000)))
    apply_event(state, envelope(3, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 51_000, 3)))
    apply_event(state, envelope(4, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 50_500, 4)))

    decision = default_engine().check(state, make_order("B", Side.BUY, 1))
    assert decision.approved is False
    assert decision.reason == RejectReason.DESK_DRAWDOWN_LIMIT
