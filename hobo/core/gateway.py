"""OrderGateway: the pre-trade path from a book's order intent to the venue. `place`
mints the order, logs ORDER_REQUEST, runs the RiskEngine, logs GATE_DECISION, and
submits to the OrderManager if approved - the one place the gate runs in live flow.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from hobo.core.state_store import StateStore
from hobo.execution.manager import OrderManager
from hobo.log.events import GateDecisionLog, OrderRequest
from hobo.risk.gate.engine import RiskEngine
from hobo.risk.model import GateDecision, Order, Side

GateDecisionHook = Callable[[str, bool, str, int], None]


class OrderGateway:
    def __init__(
        self,
        store: StateStore,
        risk_engine: RiskEngine,
        execution: OrderManager,
        on_gate_decision: GateDecisionHook | None = None,
    ) -> None:
        self._store = store
        self._risk = risk_engine
        self._execution = execution
        self._on_gate_decision = on_gate_decision
        self._order_seq = 0

    def place(self, book_id: str, instrument_id: str, side: Side, qty: float, order_type: str = "MARKET") -> GateDecision:
        order = self._build_order(book_id, instrument_id, side, qty, order_type)
        self._store.record(
            OrderRequest(order.order_id, order.book_id, order.instrument_id, order.side.value, order.qty, order.order_type)
        )

        start = time.perf_counter_ns()
        decision = self._risk.check(self._store.state, order)
        latency_ns = time.perf_counter_ns() - start

        self._store.record(
            GateDecisionLog(order.order_id, order.book_id, decision.approved, decision.reason.value, decision.detail)
        )
        if self._on_gate_decision is not None:
            self._on_gate_decision(order.book_id, decision.approved, decision.reason.value, latency_ns)

        if decision.approved:
            self._execution.submit(order, self._store.state.mark(order.instrument_id))
        return decision

    def cancel(self, order_id: str) -> None:
        self._execution.cancel(order_id)

    def _build_order(self, book_id: str, instrument_id: str, side: Side, qty: float, order_type: str) -> Order:
        self._order_seq += 1
        return Order(
            order_id=f"{book_id}-{self._order_seq}",
            book_id=book_id,
            instrument_id=instrument_id,
            side=side,
            qty=qty,
            ts_ns=time.time_ns(),
            order_type=order_type,
        )
