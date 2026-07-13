"""Pre-trade risk checks: one composable rule per limit, run in order by the
RiskEngine. Each check is pure and zero-I/O - it reads state and a precomputed
`OrderContext` (see context.py) and returns a CheckResult; a new limit is a new
RiskCheck class, not a new branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from hobo.risk.gate.context import OrderContext
from hobo.risk.model import Limit, RejectReason
from hobo.risk.state import State


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    reason: RejectReason = RejectReason.NONE
    detail: dict = field(default_factory=dict)


PASSED = CheckResult(passed=True)


def _reject(reason: RejectReason, detail: dict) -> CheckResult:
    return CheckResult(passed=False, reason=reason, detail=detail)


class RiskCheck(Protocol):
    def evaluate(self, state: State, ctx: OrderContext) -> CheckResult: ...


class KillSwitchCheck:
    def evaluate(self, state: State, ctx: OrderContext) -> CheckResult:
        desk = state.desk
        if desk.kill_switch:
            return _reject(RejectReason.KILL_SWITCH, {"scope": "DESK", "reason": desk.kill_switch_reason})
        if desk.books[ctx.book_id].kill_switch:
            return _reject(RejectReason.KILL_SWITCH, {"scope": "BOOK"})
        return PASSED


class FatFingerCheck:
    """Order-level erroneous-order guard: reject an implausibly large single order."""

    def __init__(self, max_order_size: float) -> None:
        self.max_order_size = max_order_size

    def evaluate(self, state: State, ctx: OrderContext) -> CheckResult:
        if ctx.order.qty > self.max_order_size:
            return _reject(RejectReason.FAT_FINGER, {"qty": ctx.order.qty, "limit": self.max_order_size})
        return PASSED


class BookPositionCheck:
    def evaluate(self, state: State, ctx: OrderContext) -> CheckResult:
        limit = state.desk.books[ctx.book_id].limits[Limit.POSITION]
        if abs(ctx.new_qty) > limit:
            return _reject(RejectReason.BOOK_POSITION_LIMIT, {"new_qty": ctx.new_qty, "limit": limit})
        return PASSED


class BookNotionalCheck:
    def evaluate(self, state: State, ctx: OrderContext) -> CheckResult:
        limit = state.desk.books[ctx.book_id].limits[Limit.NOTIONAL]
        if ctx.new_book_notional > limit:
            return _reject(RejectReason.BOOK_NOTIONAL_LIMIT, {"new_notional": ctx.new_book_notional, "limit": limit})
        return PASSED


class BookDrawdownCheck:
    def evaluate(self, state: State, ctx: OrderContext) -> CheckResult:
        book = state.desk.books[ctx.book_id]
        limit = book.limits[Limit.DRAWDOWN]
        drawdown = max(0.0, book.high_water_mark_usdt - ctx.hypothetical_book_pnl)
        if drawdown > limit:
            return _reject(RejectReason.BOOK_DRAWDOWN_LIMIT, {"new_drawdown": drawdown, "limit": limit})
        return PASSED


class DeskGrossNotionalCheck:
    def evaluate(self, state: State, ctx: OrderContext) -> CheckResult:
        limit = state.desk.limits[Limit.NOTIONAL]
        if ctx.new_desk_gross_notional > limit:
            return _reject(
                RejectReason.DESK_GROSS_NOTIONAL_LIMIT,
                {"desk_gross_notional": ctx.new_desk_gross_notional, "limit": limit},
            )
        return PASSED


class DeskDrawdownCheck:
    def evaluate(self, state: State, ctx: OrderContext) -> CheckResult:
        limit = state.desk.limits[Limit.DRAWDOWN]
        drawdown = max(0.0, state.desk.high_water_mark_usdt - ctx.hypothetical_desk_pnl)
        if drawdown > limit:
            return _reject(RejectReason.DESK_DRAWDOWN_LIMIT, {"desk_drawdown": drawdown, "limit": limit})
        return PASSED
