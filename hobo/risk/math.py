"""Pure PnL / exposure / liquidation math for a linear (USDT-margined) perpetual.
No I/O, no state, so it runs inside the pre-trade gate. Liquidation price assumes
isolated margin at full stated max leverage - a documented simplification, since an
all-public-data prototype has no account/margin-mode data.
"""

from __future__ import annotations

from hobo.risk.model import Instrument


def notional(qty: float, mark: float, instrument: Instrument) -> float:
    """USDT notional of a (signed) position size at a given mark price."""
    return abs(qty) * instrument.multiplier * mark


def unrealized_pnl(qty: float, entry_price: float, mark: float, instrument: Instrument) -> float:
    """Mark-to-market PnL in USDT. Signed qty handles long/short with one formula."""
    return qty * instrument.multiplier * (mark - entry_price)


def liquidation_price(qty: float, entry_price: float, instrument: Instrument) -> float | None:
    """Isolated-margin liquidation price, or None for a flat position.

    Derived from equity(m) == maintenance(m) at full max leverage:
        equity(m)      = position_margin + qty * C * (m - entry_price)
        maintenance(m) = abs(qty) * C * m * maintenance_margin_rate
    where C = instrument.multiplier and position_margin = notional(entry) / max_leverage.
    """
    if qty == 0:
        return None

    c = instrument.multiplier
    r = instrument.maintenance_margin_rate
    position_margin = abs(qty) * c * entry_price / instrument.max_leverage

    denom = c * (qty - abs(qty) * r)
    if denom == 0:
        return None
    return (qty * c * entry_price - position_margin) / denom


def liquidation_distance_pct(mark: float, liq_price: float | None) -> float | None:
    """Fractional distance from the current mark to the liquidation price."""
    if liq_price is None or mark == 0:
        return None
    return abs(mark - liq_price) / mark


def simulate_fill(
    qty_before: float,
    entry_before: float,
    delta: float,
    fill_price: float,
    instrument: Instrument,
) -> tuple[float, float, float]:
    """Pure fill simulation: returns (new_qty, new_avg_entry_price, realized_pnl_delta).

    `delta` is the signed contract quantity being added (+ for buy, - for sell).
    Shared by the state fold (which applies it) and the gate (which only previews
    it - the gate must never mutate state).
    """
    same_direction = qty_before == 0 or (qty_before > 0) == (delta > 0)
    new_qty = qty_before + delta

    if same_direction:
        new_entry_price = entry_before
        if new_qty != 0:
            new_entry_price = (abs(qty_before) * entry_before + abs(delta) * fill_price) / abs(new_qty)
        return new_qty, new_entry_price, 0.0

    closed_amount = min(abs(delta), abs(qty_before))
    closed_signed = (1 if qty_before > 0 else -1) * closed_amount
    realized_delta = unrealized_pnl(qty=closed_signed, entry_price=entry_before, mark=fill_price, instrument=instrument)

    if abs(delta) > abs(qty_before):
        new_entry_price = fill_price  # flipped: leftover opens fresh at fill price
    elif new_qty == 0:
        new_entry_price = 0.0
    else:
        new_entry_price = entry_before

    return new_qty, new_entry_price, realized_delta
