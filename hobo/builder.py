"""Assemble the engine graph from config + an exchange adapter. `build_application`
is the entry point; `EngineBuilder` is the composition root, where the order of
`build()` is the wiring order.
"""

from __future__ import annotations

import logging

from hobo.adapters.base import ExchangeAdapter
from hobo.adapters.factory import build_adapter
from hobo.application import Application, EngineServices, ReplicaApplication
from hobo.config import Config
from hobo.core.bus import EventBus
from hobo.core.events import FillEvent, FundingEvent, MarkEvent, OrderUpdateEvent
from hobo.core.exchange_reconciler import ExchangeReconciler
from hobo.core.fill_ingestor import FillIngestor
from hobo.core.fill_reconciler import FillReconciler
from hobo.core.gateway import OrderGateway
from hobo.core.order_bridge import OrderBridge
from hobo.core.health import FeedHealthMonitor
from hobo.core.recorder import Recorder
from hobo.core.state_store import StateStore
from hobo.core.strategy_runner import StrategyRunner
from hobo.core.trade_log import TradeLog
from hobo.domain.seed import load_desk_seed
from hobo.execution.manager import OrderManager
from hobo.execution.paper import PaperExecutionClient
from hobo.log.recovery import RecoveryResult, recover_state
from hobo.log.writer import LogWriter
from hobo.obs.metrics import ColdPathMetrics
from hobo.obs.server import ColdPathServer
from hobo.risk.gate.engine import default_engine
from hobo.risk.model import Instrument
from hobo.risk.staleness import StalenessWatchdog
from hobo.risk.state import State
from hobo.strategies.oscillator import OscillatorStrategy

logger = logging.getLogger(__name__)


def build_application(config: Config) -> Application | ReplicaApplication:
    adapter = build_adapter(config.exchange)
    if config.replica_mode:
        return _build_replica(config, adapter)
    return EngineBuilder(config, adapter).build()


class EngineBuilder:
    """Composition root for the primary engine (see module docstring)."""

    def __init__(self, config: Config, adapter: ExchangeAdapter) -> None:
        self.config = config
        self.adapter = adapter

    def build(self) -> Application:
        self._persistence()  # writer, recovery, store
        self._core()  # metrics, bus, fill-admission gate
        self._execution()  # execution client -> OrderManager
        self._pipeline()  # gateway, strategies, recorder, blotter + bus subscriptions
        self._market_data()  # feed for all instruments
        self._reconcilers()  # exchange + fill reconcilers (real account only)
        return self._application()

    def state(self) -> State:
        """Current state - the one provider handed to metrics, reconcilers, server."""
        return self._store.state

    def _persistence(self) -> None:
        # LogWriter opens first so it recovers/commits any crash tail before replay.
        self._writer = LogWriter(self.config.durability.event_log_path)
        self._recovery = _recover(self.config, self.adapter)
        self._store = StateStore(self._recovery.state, self._writer)

    def _core(self) -> None:
        self._metrics = ColdPathMetrics()
        self._metrics.register_state_metrics(self.state)
        self._bus = EventBus(on_dispatch_ns=self._metrics.on_event_dispatched, on_dropped=self._metrics.on_event_dropped)
        # Single admission gate for fills from any source (WS + REST reconcile); dedups by trade_id.
        self._ingestor = FillIngestor(self._bus.publish)
        self._ingestor.seed_from_log(self.config.durability.event_log_path)

    def _execution(self) -> None:
        client = PaperExecutionClient() if self.config.execution.backend == "paper" else self.adapter.execution()
        self._execution_mgr = OrderManager(client)
        self._execution_mgr.on_fill = self._ingestor.submit  # fills go through the admission gate
        self._execution_mgr.on_order_update = self._bus.publish  # order updates straight onto the bus

    def _pipeline(self) -> None:
        self._monitor = FeedHealthMonitor(self._store, StalenessWatchdog(), self._store.state.desk.desk_id)
        self._trade_log = TradeLog()
        self._trade_log.load_history(self.config.durability.event_log_path)  # recent trades immediately, not empty
        self._gateway = OrderGateway(self._store, default_engine(), self._execution_mgr, on_gate_decision=self._metrics.on_gate_decision)
        self._order_bridge = OrderBridge(self._gateway)  # manual order entry from the dashboard
        runner = StrategyRunner(_build_strategies(self.state()), self._store, self._gateway)
        recorder = Recorder(self._store)

        # Subscription topology as data; order matters: fold (recorder) -> feed health -> blotter -> strategies.
        topology = [
            (recorder.record, (MarkEvent, FundingEvent, FillEvent)),
            (self._monitor.on_message, (MarkEvent, FundingEvent)),
            (self._trade_log.record, (FillEvent,)),
            (runner.handle, (MarkEvent, FundingEvent, FillEvent, OrderUpdateEvent)),
        ]
        for handler, event_types in topology:
            for event_type in event_types:
                self._bus.subscribe(event_type, handler)

        for warning in self._recovery.warnings:  # persist reconciliation warnings to the log
            self._store.record(warning)

    def _market_data(self) -> None:
        self.adapter.market_data().stream(self._bus, self.config.exchange.instrument_ids)

    def _reconcilers(self) -> None:
        # Reconcile against the exchange only when trading a real account (paper has none).
        self._reconciler: ExchangeReconciler | None = None
        self._fill_reconciler: FillReconciler | None = None
        account = self.adapter.account()
        if self.config.execution.backend == "exchange" and account is not None:
            self._reconciler = ExchangeReconciler(account, self.state)
            self._fill_reconciler = FillReconciler(account, self._ingestor, self.state)

    def _application(self) -> Application:
        server = ColdPathServer(
            self.config.coldpath.metrics_port,
            self._metrics.registry,
            state_provider=self.state,
            trades_provider=self._trade_log.recent,
            exchange_provider=(self._reconciler.latest if self._reconciler else None),
            order_submitter=self._order_bridge.place,
        )
        services = EngineServices(
            store=self._store,
            writer=self._writer,
            monitor=self._monitor,
            cold_path_server=server,
            order_bridge=self._order_bridge,
            reconciler=self._reconciler,
            fill_reconciler=self._fill_reconciler,
        )
        return Application(self.config.durability, self.adapter, services)


def _build_replica(config: Config, adapter: ExchangeAdapter) -> ReplicaApplication:
    state = _recover(config, adapter).state
    metrics = ColdPathMetrics()
    cold_path_server = ColdPathServer(config.coldpath.metrics_port, metrics.registry, state_provider=lambda: state)
    return ReplicaApplication(config.durability, adapter, state, cold_path_server)


def _recover(config: Config, adapter: ExchangeAdapter) -> RecoveryResult:
    reference = adapter.reference()
    market_data = adapter.market_data()
    instruments = {
        instrument_id: Instrument.from_spec(
            reference.fetch_contract_spec(instrument_id), config.risk.maintenance_margin_rate
        )
        for instrument_id in config.exchange.instrument_ids
    }
    seed = load_desk_seed(config.durability.desk_seed_path)
    book_specs = [(book.book_id, book.limits) for book in seed.books]

    def fresh_state_factory() -> State:
        return State.initial(instruments, desk_id=seed.desk_id, desk_limits=seed.limits, book_specs=book_specs)

    result = recover_state(
        config.durability.event_log_path,
        config.durability.snapshot_path,
        fresh_state_factory,
        reference_client=reference,
        market_client=market_data,
        book_specs=book_specs,
    )
    logger.info(
        "recovered state: from_snapshot=%s replayed=%d warnings=%d",
        result.recovered_from_snapshot,
        result.replayed_event_count,
        len(result.warnings),
    )
    return result


def _build_strategies(state: State) -> list[tuple[str, object]]:
    # Each (book_id, strategy) pair trades its own book across the instrument universe; strategies are book-agnostic.
    instrument_ids = list(state.instruments)
    pairs = [
        ("scalper", OscillatorStrategy(instrument_ids)),
    ]
    for book_id, _ in pairs:
        if book_id not in state.desk.books:
            raise ValueError(f"strategy book {book_id!r} not in seed books {sorted(state.desk.books)}")
    return pairs
