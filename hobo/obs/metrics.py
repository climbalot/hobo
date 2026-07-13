"""Cold-path metrics: Prometheus counters/histograms fed by pipeline callbacks,
registered against a dedicated CollectorRegistry (not the process-global default)
so multiple instances - notably one per test - never collide.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily

if TYPE_CHECKING:
    from hobo.risk.state import State

GATE_LATENCY_BUCKETS = (
    0.000_01,
    0.000_025,
    0.000_05,
    0.000_1,
    0.000_25,
    0.000_5,
    0.001,
    0.0025,
    0.005,
    0.01,
)
DISPATCH_LATENCY_BUCKETS = (
    0.000_05,
    0.000_1,
    0.000_25,
    0.000_5,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
)


class ColdPathMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.gate_decisions_total = Counter(
            "gate_decisions_total",
            "Pre-trade gate decisions by book, approval, and reject reason.",
            ["book", "decision", "reason"],
            registry=self.registry,
        )
        self.gate_check_latency_seconds = Histogram(
            "gate_check_latency_seconds",
            "Per-order gate check latency.",
            buckets=GATE_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.event_dispatch_seconds = Histogram(
            "event_dispatch_seconds",
            "Time to dispatch one bus event through all its handlers, by event type "
            "(event_type=MarkEvent is the tick-to-state-update path).",
            ["event_type"],
            buckets=DISPATCH_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.event_bus_dropped_total = Counter(
            "event_bus_dropped_total",
            "Events dropped by the bus because the queue was full (backpressure safety valve).",
            ["event_type"],
            registry=self.registry,
        )

    def on_gate_decision(self, book_id: str, approved: bool, reason: str, latency_ns: int) -> None:
        decision = "approved" if approved else "rejected"
        self.gate_decisions_total.labels(book=book_id, decision=decision, reason=reason).inc()
        self.gate_check_latency_seconds.observe(latency_ns / 1e9)

    def on_event_dispatched(self, event_type: type, latency_ns: int) -> None:
        self.event_dispatch_seconds.labels(event_type=event_type.__name__).observe(latency_ns / 1e9)

    def on_event_dropped(self, event_type: type) -> None:
        self.event_bus_dropped_total.labels(event_type=event_type.__name__).inc()

    def register_state_metrics(self, state_provider: Callable[[], State]) -> None:
        """Expose live PnL/drawdown/position, computed from state at scrape time."""
        self.registry.register(_StateMetricsCollector(state_provider))


class _StateMetricsCollector:
    """prometheus custom collector: reads risk state on each /metrics scrape (cold
    path, no hot-path cost) and emits per-book and desk PnL / drawdown / position."""

    def __init__(self, state_provider: Callable[[], State]) -> None:
        self._state_provider = state_provider

    def collect(self):
        state = self._state_provider()

        book_pnl = GaugeMetricFamily("book_pnl_usdt", "Book total PnL, net of fees (realized - fees + unrealized).", labels=["book"])
        book_realized = GaugeMetricFamily("book_realized_pnl_usdt", "Book realized PnL, gross of fees (closing fills + funding).", labels=["book"])
        book_unrealized = GaugeMetricFamily("book_unrealized_pnl_usdt", "Book unrealized (mark-to-market) PnL.", labels=["book"])
        book_fees = GaugeMetricFamily("book_fees_paid_usdt", "Cumulative trade fees paid by the book.", labels=["book"])
        book_drawdown = GaugeMetricFamily("book_drawdown_usdt", "Book drawdown from its high-water mark.", labels=["book"])
        book_position = GaugeMetricFamily("book_position_qty", "Book position, signed contracts.", labels=["book", "instrument"])
        for book_id, book in state.desk.books.items():
            book_pnl.add_metric([book_id], state.book_total_pnl(book))
            book_realized.add_metric([book_id], state.book_realized_pnl(book))
            book_unrealized.add_metric([book_id], state.book_unrealized_pnl(book))
            book_fees.add_metric([book_id], state.book_fees_paid(book))
            book_drawdown.add_metric([book_id], state.book_drawdown(book))
            for iid, pos in book.positions.items():
                book_position.add_metric([book_id, iid], pos.qty)
        yield book_pnl
        yield book_realized
        yield book_unrealized
        yield book_fees
        yield book_drawdown
        yield book_position

        desk_pnl = GaugeMetricFamily("desk_pnl_usdt", "Desk total PnL across books.")
        desk_pnl.add_metric([], state.desk_total_pnl())
        yield desk_pnl
        desk_drawdown = GaugeMetricFamily("desk_drawdown_usdt", "Desk drawdown from its high-water mark.")
        desk_drawdown.add_metric([], state.desk_drawdown())
        yield desk_drawdown
