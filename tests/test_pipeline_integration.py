"""End-to-end wiring of the live hot path: EventBus -> Recorder (fold+log) ->
StrategyRunner -> OrderGateway -> RiskEngine -> OrderManager -> PaperExecutionClient,
with fills flowing back through the FillIngestor onto the bus. This is the same
topology the EngineBuilder composes, assembled directly so a scripted mark sequence
can be driven through it.
"""

from __future__ import annotations

from hobo.core.bus import EventBus
from hobo.core.events import FillEvent, FundingEvent, MarkEvent, OrderUpdateEvent, PlaceOrder
from hobo.core.fill_ingestor import FillIngestor
from hobo.core.gateway import OrderGateway
from hobo.core.recorder import Recorder
from hobo.core.state_store import StateStore
from hobo.core.strategy_runner import StrategyRunner
from hobo.execution.manager import OrderManager
from hobo.execution.paper import PaperExecutionClient
from hobo.log import reader
from hobo.log.events import EventType, KillSwitch, ReconciliationWarning
from hobo.log.writer import LogWriter
from hobo.risk.gate.engine import default_engine
from hobo.risk.model import Order, RejectReason, Side

from conftest import INSTRUMENT_ID, make_state


class _ThresholdStrategy:
    """Minimal driver for the pipeline test: buys/sells one contract when the mark
    moves past a bps threshold. Keeps this test about the wiring, not strategy logic."""

    def __init__(self, instrument_id, threshold_bps=5.0, qty=1.0):
        self.instrument_id = instrument_id
        self.threshold_bps = threshold_bps
        self.qty = qty
        self._last = None

    def on_event(self, event, state):
        if not isinstance(event, MarkEvent) or event.instrument_id != self.instrument_id:
            return []
        prev, self._last = self._last, event.mark_price
        if prev is None:
            return []
        move_bps = (event.mark_price - prev) / prev * 10_000
        if move_bps > self.threshold_bps:
            return [PlaceOrder(instrument_id=self.instrument_id, side=Side.BUY, qty=self.qty)]
        if move_bps < -self.threshold_bps:
            return [PlaceOrder(instrument_id=self.instrument_id, side=Side.SELL, qty=self.qty)]
        return []


class Wiring:
    def __init__(self, tmp_path, instrument):
        self.log_path = str(tmp_path / "eventlog.bin")
        self.writer = LogWriter(self.log_path)
        self.store = StateStore(make_state(instrument), self.writer)
        self.bus = EventBus()
        self.ingestor = FillIngestor(self.bus.publish)

        client = PaperExecutionClient()
        self.manager = OrderManager(client)
        self.manager.on_fill = self.ingestor.submit
        self.manager.on_order_update = self.bus.publish

        self.gate_decisions: list[tuple] = []
        self.gateway = OrderGateway(
            self.store, default_engine(), self.manager, on_gate_decision=self._record_decision
        )
        runner = StrategyRunner(
            [("A", _ThresholdStrategy(INSTRUMENT_ID, threshold_bps=5.0, qty=1.0))], self.store, self.gateway
        )
        recorder = Recorder(self.store)

        # fold (recorder) must run before strategies react.
        for handler, types in [
            (recorder.record, (MarkEvent, FundingEvent, FillEvent)),
            (runner.handle, (MarkEvent, FundingEvent, FillEvent, OrderUpdateEvent)),
        ]:
            for event_type in types:
                self.bus.subscribe(event_type, handler)

    def _record_decision(self, book_id, approved, reason, latency_ns):
        self.gate_decisions.append((book_id, approved, reason, latency_ns))

    @property
    def state(self):
        return self.store.state

    def replay_event_types(self) -> list[str]:
        self.writer.commit()
        return [e.event_type for e in reader.replay(reader.open_readonly(self.log_path))]


def test_full_tick_sequence_produces_expected_fills_and_log(tmp_path, instrument):
    w = Wiring(tmp_path, instrument)

    w.bus.publish(MarkEvent(INSTRUMENT_ID, 50_000, 1))  # first tick: no order
    w.bus.publish(MarkEvent(INSTRUMENT_ID, 50_030, 2))  # +6bps: A buys 1
    w.bus.publish(MarkEvent(INSTRUMENT_ID, 50_000, 3))  # ~-6bps: A sells 1 back to flat

    assert w.state.desk.books["A"].position(INSTRUMENT_ID).qty == 0
    assert len(w.gate_decisions) == 2
    assert all(approved for _, approved, _, _ in w.gate_decisions)

    assert w.replay_event_types() == [
        EventType.MARK_UPDATE,
        EventType.MARK_UPDATE,
        EventType.ORDER_REQUEST,
        EventType.GATE_DECISION,
        EventType.FILL,
        EventType.MARK_UPDATE,
        EventType.ORDER_REQUEST,
        EventType.GATE_DECISION,
        EventType.FILL,
    ]
    w.writer.close()


def test_desk_kill_switch_rejects_strategy_orders(tmp_path, instrument):
    w = Wiring(tmp_path, instrument)
    w.store.record(KillSwitch("DESK", w.state.desk.desk_id, True, "feed stale"))

    w.bus.publish(MarkEvent(INSTRUMENT_ID, 50_000, 1))
    w.bus.publish(MarkEvent(INSTRUMENT_ID, 50_030, 2))  # would trigger a BUY

    assert len(w.gate_decisions) == 1
    book_id, approved, reason, _ = w.gate_decisions[0]
    assert approved is False
    assert reason == RejectReason.KILL_SWITCH.value
    assert w.state.desk.books["A"].position(INSTRUMENT_ID).qty == 0
    w.writer.close()


def test_funding_tick_applies_without_generating_orders(tmp_path, instrument):
    w = Wiring(tmp_path, instrument)
    w.bus.publish(MarkEvent(INSTRUMENT_ID, 50_000, 1))
    w.bus.publish(FundingEvent(INSTRUMENT_ID, 0.0005, 2000, 2))
    assert w.state.funding_rates[INSTRUMENT_ID] == 0.0005
    assert w.gate_decisions == []
    w.writer.close()


def test_manual_order_via_gateway_bypasses_strategies(tmp_path, instrument):
    w = Wiring(tmp_path, instrument)
    w.bus.publish(MarkEvent(INSTRUMENT_ID, 50_000, 1))
    decision = w.gateway.place("A", INSTRUMENT_ID, Side.BUY, 2)
    assert decision.approved is True
    assert w.state.desk.books["A"].position(INSTRUMENT_ID).qty == 2
    w.writer.close()


def test_order_type_flows_through_to_the_order(tmp_path, instrument):
    # Sanity that the Order model the gateway mints is well-formed.
    order = Order("m1", "A", INSTRUMENT_ID, Side.BUY, 1, ts_ns=1)
    assert order.signed_qty == 1
    assert order.order_type == "MARKET"


def test_reconciliation_warning_is_appended_to_log(tmp_path, instrument):
    w = Wiring(tmp_path, instrument)
    w.store.record(ReconciliationWarning("MARK_STALE", {"age_ns": 6_000_000_000}))
    w.writer.commit()
    events = reader.replay(reader.open_readonly(w.log_path))
    assert events[-1].event_type == EventType.RECONCILIATION_WARNING
    assert events[-1].payload["kind"] == "MARK_STALE"
    w.writer.close()
