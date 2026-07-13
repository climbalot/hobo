from __future__ import annotations

import pytest

from hobo.risk.math import liquidation_distance_pct, liquidation_price, notional, unrealized_pnl
from hobo.risk.model import Instrument

from conftest import make_instrument


@pytest.fixture
def btc_perp() -> Instrument:
    return make_instrument()


def test_notional_scales_by_multiplier(btc_perp):
    # 100 contracts * 0.01 BTC/contract * 1.0 mult * $50,000 mark = $50,000 notional
    assert notional(qty=100, mark=50_000, instrument=btc_perp) == pytest.approx(50_000.0)

    double_mult = btc_perp.__class__(**{**btc_perp.__dict__, "ct_mult": 2.0})
    assert notional(qty=100, mark=50_000, instrument=double_mult) == pytest.approx(100_000.0)


def test_notional_is_sign_independent(btc_perp):
    long_notional = notional(qty=100, mark=50_000, instrument=btc_perp)
    short_notional = notional(qty=-100, mark=50_000, instrument=btc_perp)
    assert long_notional == short_notional


def test_unrealized_pnl_long_gains_when_mark_rises(btc_perp):
    pnl = unrealized_pnl(qty=100, entry_price=50_000, mark=51_000, instrument=btc_perp)
    # 100 contracts * 0.01 BTC * $1,000 move = $1,000
    assert pnl == pytest.approx(1_000.0)


def test_unrealized_pnl_long_loses_when_mark_falls(btc_perp):
    pnl = unrealized_pnl(qty=100, entry_price=50_000, mark=49_000, instrument=btc_perp)
    assert pnl == pytest.approx(-1_000.0)


def test_unrealized_pnl_short_gains_when_mark_falls(btc_perp):
    pnl = unrealized_pnl(qty=-100, entry_price=50_000, mark=49_000, instrument=btc_perp)
    assert pnl == pytest.approx(1_000.0)


def test_unrealized_pnl_short_loses_when_mark_rises(btc_perp):
    pnl = unrealized_pnl(qty=-100, entry_price=50_000, mark=51_000, instrument=btc_perp)
    assert pnl == pytest.approx(-1_000.0)


def test_unrealized_pnl_flat_is_zero(btc_perp):
    assert unrealized_pnl(qty=0, entry_price=50_000, mark=60_000, instrument=btc_perp) == 0.0


def test_liquidation_price_flat_position_is_none(btc_perp):
    assert liquidation_price(qty=0, entry_price=50_000, instrument=btc_perp) is None


def test_liquidation_price_long_is_below_entry(btc_perp):
    liq = liquidation_price(qty=100, entry_price=50_000, instrument=btc_perp)
    assert liq is not None
    assert liq < 50_000
    # closed-form check: entry * (1 - 1/L) / (1 - R)
    expected = 50_000 * (1 - 1 / 10) / (1 - 0.005)
    assert liq == pytest.approx(expected)


def test_liquidation_price_short_is_above_entry(btc_perp):
    liq = liquidation_price(qty=-100, entry_price=50_000, instrument=btc_perp)
    assert liq is not None
    assert liq > 50_000
    expected = 50_000 * (1 + 1 / 10) / (1 + 0.005)
    assert liq == pytest.approx(expected)


def test_liquidation_distance_shrinks_as_mark_moves_toward_liquidation(btc_perp):
    liq = liquidation_price(qty=100, entry_price=50_000, instrument=btc_perp)
    far = liquidation_distance_pct(mark=50_000, liq_price=liq)
    near = liquidation_distance_pct(mark=liq + (50_000 - liq) * 0.1, liq_price=liq)
    assert 0 < near < far


def test_liquidation_distance_none_for_flat_position():
    assert liquidation_distance_pct(mark=50_000, liq_price=None) is None


def test_higher_leverage_moves_liquidation_closer_to_entry(btc_perp):
    low_lev = liquidation_price(qty=100, entry_price=50_000, instrument=btc_perp)
    high_lev_instrument = Instrument(**{**btc_perp.__dict__, "max_leverage": 20.0})
    high_lev = liquidation_price(qty=100, entry_price=50_000, instrument=high_lev_instrument)
    # more leverage -> less margin cushion -> liquidation price closer to entry
    assert high_lev > low_lev
