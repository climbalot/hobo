from __future__ import annotations

from hobo.core.events import FillEvent, MarkEvent, PlaceOrder
from hobo.risk.model import Side

from conftest import INSTRUMENT_ID

IID = INSTRUMENT_ID


def mark(price: float, ts_ns: int = 1) -> MarkEvent:
    return MarkEvent(IID, price, ts_ns)


def fill_event(side: Side, qty: float, book_id: str = "B") -> FillEvent:
    return FillEvent(
        order_id="o", book_id=book_id, instrument_id=IID, side=side.value, qty=qty, fill_price=50_000
    )


# --- momentum ---


def _momentum(**kwargs):
    from hobo.strategies.momentum import MomentumStrategy

    return MomentumStrategy([IID], **kwargs)


def test_momentum_no_order_on_first_tick(fresh_state):
    strategy = _momentum()
    assert strategy.on_event(mark(50_000), fresh_state) == []


def test_momentum_no_order_below_threshold(fresh_state):
    strategy = _momentum(threshold_bps=5.0)
    strategy.on_event(mark(50_000), fresh_state)
    # move = 10/50000*10000 = 2bps, below the 5bps threshold
    assert strategy.on_event(mark(50_010), fresh_state) == []


def test_momentum_buy_on_upward_move_past_threshold(fresh_state):
    strategy = _momentum(threshold_bps=5.0)
    strategy.on_event(mark(50_000), fresh_state)
    actions = strategy.on_event(mark(50_030), fresh_state)  # +6bps
    assert len(actions) == 1
    assert isinstance(actions[0], PlaceOrder)
    assert actions[0].side == Side.BUY
    assert actions[0].instrument_id == IID


def test_momentum_sell_on_downward_move_past_threshold(fresh_state):
    strategy = _momentum(threshold_bps=5.0)
    strategy.on_event(mark(50_030), fresh_state)
    actions = strategy.on_event(mark(50_000), fresh_state)  # ~ -6bps
    assert actions and actions[0].side == Side.SELL


def test_momentum_exact_scripted_sequence(fresh_state):
    strategy = _momentum(threshold_bps=5.0, order_qty=1.0)
    ticks = [50_000, 50_000, 50_030, 50_030, 50_000]
    sides = []
    for i, price in enumerate(ticks):
        actions = strategy.on_event(mark(price, ts_ns=i), fresh_state)
        sides.append(actions[0].side if actions else None)
    assert sides == [None, None, Side.BUY, None, Side.SELL]


def test_momentum_ignores_other_instruments(fresh_state):
    strategy = _momentum(threshold_bps=5.0)
    strategy.on_event(mark(50_000), fresh_state)
    assert strategy.on_event(MarkEvent("ETH-USDT-SWAP", 999, 2), fresh_state) == []


# --- mean reversion (fill-aware inventory) ---


def _mean_reversion(**kwargs):
    from hobo.strategies.mean_reversion import MeanReversionStrategy

    return MeanReversionStrategy([IID], **kwargs)


def test_mean_reversion_no_order_within_threshold(fresh_state):
    strategy = _mean_reversion(drift_threshold=2.0)
    strategy.on_event(fill_event(Side.BUY, 1.0), fresh_state)
    assert strategy.on_event(mark(50_000), fresh_state) == []


def test_mean_reversion_sells_when_long_past_threshold(fresh_state):
    strategy = _mean_reversion(drift_threshold=2.0, order_qty=1.0)
    strategy.on_event(fill_event(Side.BUY, 3.0), fresh_state)
    actions = strategy.on_event(mark(50_000), fresh_state)
    assert actions and actions[0].side == Side.SELL


def test_mean_reversion_buys_when_short_past_threshold(fresh_state):
    strategy = _mean_reversion(drift_threshold=2.0, order_qty=1.0)
    strategy.on_event(fill_event(Side.SELL, 3.0), fresh_state)
    actions = strategy.on_event(mark(50_000), fresh_state)
    assert actions and actions[0].side == Side.BUY


def test_mean_reversion_targets_nonzero_target_qty(fresh_state):
    strategy = _mean_reversion(target_qty=5.0, drift_threshold=2.0)
    strategy.on_event(fill_event(Side.BUY, 5.0), fresh_state)
    assert strategy.on_event(mark(50_000), fresh_state) == []

    strategy.on_event(fill_event(Side.BUY, 3.5), fresh_state)  # inventory 8.5, drift 3.5 > threshold
    actions = strategy.on_event(mark(50_000, ts_ns=2), fresh_state)
    assert actions and actions[0].side == Side.SELL


# --- oscillator (activity generator) ---


def _oscillator(**kwargs):
    from hobo.strategies.oscillator import OscillatorStrategy

    return OscillatorStrategy([IID], **kwargs)


def test_oscillator_only_fires_every_interval(fresh_state):
    strategy = _oscillator(interval_ticks=3, order_qty=0.1, position_band=1.0)
    assert strategy.on_event(mark(50_000, 1), fresh_state) == []
    assert strategy.on_event(mark(50_000, 2), fresh_state) == []
    actions = strategy.on_event(mark(50_000, 3), fresh_state)  # third tick fires
    assert len(actions) == 1
    assert isinstance(actions[0], PlaceOrder)


def test_oscillator_sells_when_long_of_band(fresh_state):
    strategy = _oscillator(interval_ticks=1, order_qty=0.1, position_band=1.0)
    strategy.on_event(fill_event(Side.BUY, 2.0), fresh_state)  # inventory above the band
    actions = strategy.on_event(mark(50_000), fresh_state)
    assert actions and actions[0].side == Side.SELL
