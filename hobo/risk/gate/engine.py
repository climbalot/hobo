"""Pre-trade risk engine: runs a pipeline of composable checks and aggregates
the result into a GateDecision.

Structured after production pre-trade risk (cf. SEC Rule 15c3-5 market-access
controls): ordered checks across order/book/desk scopes, short-circuiting on
the first hard reject. Pure and zero-I/O - it never mutates state or sends an
order; the caller submits the approved order through the execution engine.

Not modeled (lite): limit *reservation*. With async fills there is a window
where two in-flight orders both pass because neither has filled yet. See
docs/design-notes.md.
"""

from __future__ import annotations

from hobo.risk.gate.checks import (
    BookDrawdownCheck,
    BookNotionalCheck,
    BookPositionCheck,
    DeskDrawdownCheck,
    DeskGrossNotionalCheck,
    FatFingerCheck,
    KillSwitchCheck,
    RiskCheck,
)
from hobo.risk.gate.context import build_order_context
from hobo.risk.model import GateDecision, Order, RejectReason
from hobo.risk.state import State

DEFAULT_MAX_ORDER_SIZE = 1000.0


class RiskEngine:
    def __init__(self, checks: list[RiskCheck]) -> None:
        self.checks = checks

    def check(self, state: State, order: Order) -> GateDecision:
        ctx = build_order_context(state, order)
        for rule in self.checks:
            result = rule.evaluate(state, ctx)
            if not result.passed:
                return GateDecision(order_id=order.order_id, approved=False, reason=result.reason, detail=result.detail)
        return GateDecision(order_id=order.order_id, approved=True, reason=RejectReason.NONE)


def default_checks(max_order_size: float = DEFAULT_MAX_ORDER_SIZE) -> list[RiskCheck]:
    return [
        KillSwitchCheck(),
        FatFingerCheck(max_order_size),
        BookPositionCheck(),
        BookNotionalCheck(),
        BookDrawdownCheck(),
        DeskGrossNotionalCheck(),
        DeskDrawdownCheck(),
    ]


def default_engine(max_order_size: float = DEFAULT_MAX_ORDER_SIZE) -> RiskEngine:
    return RiskEngine(default_checks(max_order_size))
