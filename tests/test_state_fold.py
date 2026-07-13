from __future__ import annotations

import copy

import pytest

from hobo.log.events import EventType
from hobo.risk.fold import apply_event
from hobo.risk.model import Limit

from conftest import (
    INSTRUMENT_ID,
    envelope,
    fill,
    funding_update,
    gate_decision,
    kill_switch,
    limit_change,
    make_state,
    mark_update,
    order_request,
    reconciliation_warning,
)


def book_pos(state, book_id="A", instrument_id=INSTRUMENT_ID):
    return state.desk.books[book_id].position(instrument_id)


def test_mark_update_sets_mark(fresh_state):
    apply_event(fresh_state, envelope(1, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 50_000, 1)))
    assert fresh_state.mark(INSTRUMENT_ID) == 50_000
    assert fresh_state.mark_ts_ns[INSTRUMENT_ID] == 1
    assert fresh_state.last_seq == 1


def test_fill_opens_position_from_flat(fresh_state):
    apply_event(fresh_state, envelope(1, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000)))
    pos = book_pos(fresh_state)
    assert pos.qty == 10
    assert pos.avg_entry_price == 50_000


def test_fill_same_direction_add_averages_entry_price(fresh_state):
    apply_event(fresh_state, envelope(1, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000)))
    apply_event(fresh_state, envelope(2, EventType.FILL, fill("o2", "A", INSTRUMENT_ID, "BUY", 10, 52_000)))
    pos = book_pos(fresh_state)
    assert pos.qty == 20
    assert pos.avg_entry_price == pytest.approx(51_000)


def test_fill_partial_reduce_keeps_entry_price_realizes_pnl(fresh_state):
    apply_event(fresh_state, envelope(1, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000)))
    apply_event(fresh_state, envelope(2, EventType.FILL, fill("o2", "A", INSTRUMENT_ID, "SELL", 4, 51_000)))
    pos = book_pos(fresh_state)
    assert pos.qty == 6
    assert pos.avg_entry_price == pytest.approx(50_000)
    # 4 contracts closed at +$1,000 move * 0.01 ct_val = $40 realized
    assert pos.realized_pnl == pytest.approx(40.0)


def test_fill_full_close_zeroes_position(fresh_state):
    apply_event(fresh_state, envelope(1, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000)))
    apply_event(fresh_state, envelope(2, EventType.FILL, fill("o2", "A", INSTRUMENT_ID, "SELL", 10, 51_000)))
    pos = book_pos(fresh_state)
    assert pos.qty == 0
    assert pos.avg_entry_price == 0.0
    assert pos.realized_pnl == pytest.approx(100.0)


def test_fill_flip_opens_new_position_at_fill_price(fresh_state):
    apply_event(fresh_state, envelope(1, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000)))
    apply_event(fresh_state, envelope(2, EventType.FILL, fill("o2", "A", INSTRUMENT_ID, "SELL", 15, 51_000)))
    pos = book_pos(fresh_state)
    assert pos.qty == -5
    assert pos.avg_entry_price == pytest.approx(51_000)
    assert pos.realized_pnl == pytest.approx(100.0)


def test_fill_records_fee(fresh_state):
    apply_event(fresh_state, envelope(1, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000, fee=2.5)))
    assert book_pos(fresh_state).fees_paid == pytest.approx(2.5)


def test_funding_positive_rate_debits_long_credits_short(fresh_state):
    apply_event(fresh_state, envelope(1, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 50_000, 1)))
    apply_event(fresh_state, envelope(2, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000)))
    apply_event(fresh_state, envelope(3, EventType.FILL, fill("o2", "B", INSTRUMENT_ID, "SELL", 10, 50_000)))

    apply_event(fresh_state, envelope(4, EventType.FUNDING_UPDATE, funding_update(INSTRUMENT_ID, 0.001, 4000)))

    long_pos = book_pos(fresh_state, "A")
    short_pos = book_pos(fresh_state, "B")
    assert long_pos.funding_pnl < 0  # long pays
    assert short_pos.funding_pnl > 0  # short receives
    assert long_pos.funding_pnl == pytest.approx(-short_pos.funding_pnl)
    assert fresh_state.funding_rates[INSTRUMENT_ID] == 0.001


def test_limit_change_updates_book_limits(fresh_state):
    apply_event(
        fresh_state,
        envelope(1, EventType.LIMIT_CHANGE, limit_change("BOOK", "NOTIONAL", 500_000, 400_000, book_id="A")),
    )
    assert fresh_state.desk.books["A"].limits[Limit.NOTIONAL] == 400_000


def test_limit_change_updates_desk_limits(fresh_state):
    apply_event(
        fresh_state,
        envelope(1, EventType.LIMIT_CHANGE, limit_change("DESK", "NOTIONAL", 700_000, 600_000)),
    )
    assert fresh_state.desk.limits[Limit.NOTIONAL] == 600_000


def test_kill_switch_book_scope(fresh_state):
    apply_event(fresh_state, envelope(1, EventType.KILL_SWITCH, kill_switch("BOOK", "A", True, "manual halt")))
    assert fresh_state.desk.books["A"].kill_switch is True
    assert fresh_state.desk.books["B"].kill_switch is False


def test_kill_switch_desk_scope(fresh_state):
    apply_event(fresh_state, envelope(1, EventType.KILL_SWITCH, kill_switch("DESK", "desk-1", True, "feed stale")))
    assert fresh_state.desk.kill_switch is True
    assert fresh_state.desk.kill_switch_reason == "feed stale"


def test_high_water_mark_and_drawdown_track_pnl_decline(fresh_state):
    apply_event(fresh_state, envelope(1, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000)))
    apply_event(fresh_state, envelope(2, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 52_000, 2)))
    book = fresh_state.desk.books["A"]
    assert fresh_state.book_drawdown(book) == pytest.approx(0.0)
    peak_hwm = book.high_water_mark_usdt
    assert peak_hwm == pytest.approx(200.0)  # 10 * 0.01 * $2,000 move

    apply_event(fresh_state, envelope(3, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 51_000, 3)))
    assert book.high_water_mark_usdt == pytest.approx(peak_hwm)  # HWM doesn't fall
    assert fresh_state.book_drawdown(book) == pytest.approx(100.0)  # dropped from +200 to +100


def test_audit_only_events_do_not_mutate_state(fresh_state):
    before = fresh_state.to_dict()
    apply_event(fresh_state, envelope(1, EventType.ORDER_REQUEST, order_request("o1", "A", INSTRUMENT_ID, "BUY", 5)))
    apply_event(fresh_state, envelope(2, EventType.GATE_DECISION, gate_decision("o1", "A", True, "NONE")))
    apply_event(
        fresh_state,
        envelope(3, EventType.RECONCILIATION_WARNING, reconciliation_warning("MARK_STALE", {"age_ms": 900})),
    )
    after = fresh_state.to_dict()
    after["last_seq"] = before["last_seq"]  # last_seq is expected to advance
    assert after == before


def test_unknown_event_type_raises(fresh_state):
    with pytest.raises(ValueError, match="unknown event_type"):
        apply_event(fresh_state, envelope(1, "NOT_A_REAL_EVENT", {}))


def test_fold_is_deterministic_given_same_event_sequence(instrument):
    sequence = [
        envelope(1, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 50_000, 1)),
        envelope(2, EventType.FILL, fill("o1", "A", INSTRUMENT_ID, "BUY", 10, 50_000)),
        envelope(3, EventType.FILL, fill("o2", "B", INSTRUMENT_ID, "SELL", 5, 50_000)),
        envelope(4, EventType.MARK_UPDATE, mark_update(INSTRUMENT_ID, 51_500, 4)),
        envelope(5, EventType.FUNDING_UPDATE, funding_update(INSTRUMENT_ID, 0.0005, 5000)),
        envelope(6, EventType.FILL, fill("o3", "A", INSTRUMENT_ID, "SELL", 3, 51_500)),
    ]

    state_1 = make_state(instrument)
    state_2 = make_state(instrument)
    for env in copy.deepcopy(sequence):
        apply_event(state_1, env)
    for env in copy.deepcopy(sequence):
        apply_event(state_2, env)

    assert state_1.to_dict() == state_2.to_dict()
