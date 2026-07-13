from __future__ import annotations

from hobo.core.events import FillEvent, MarkEvent, PlaceOrder
from hobo.risk.model import Side

from conftest import INSTRUMENT_ID

IID = INSTRUMENT_ID


def mark(price: float, ts_ns: int = 1) -> MarkEvent:
    return MarkEvent(IID, price, ts_ns)


def fill_event(side: Side, qty: float, book_id: str = "scalper") -> FillEvent:
    return FillEvent(
        order_id="o", book_id=book_id, instrument_id=IID, side=side.value, qty=qty, fill_price=50_000
    )


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


def test_oscillator_ignores_other_instruments(fresh_state):
    strategy = _oscillator(interval_ticks=1)
    assert strategy.on_event(MarkEvent("ETH-USDT-SWAP", 999, 1), fresh_state) == []


def test_oscillator_sells_when_long_of_band(fresh_state):
    strategy = _oscillator(interval_ticks=1, order_qty=0.1, position_band=1.0)
    strategy.on_event(fill_event(Side.BUY, 2.0), fresh_state)  # inventory above the band
    actions = strategy.on_event(mark(50_000), fresh_state)
    assert actions and actions[0].side == Side.SELL


def test_oscillator_buys_when_short_of_band(fresh_state):
    strategy = _oscillator(interval_ticks=1, order_qty=0.1, position_band=1.0)
    strategy.on_event(fill_event(Side.SELL, 2.0), fresh_state)  # inventory below the band
    actions = strategy.on_event(mark(50_000), fresh_state)
    assert actions and actions[0].side == Side.BUY
