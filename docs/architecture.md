# Architecture

This expands the diagram in the README with the actual module that implements each piece, for anyone navigating the code rather than just reading about it.

## Component map

| Diagram component | Module(s) | Notes |
|---|---|---|
| Adapter interface + factory | `hobo/adapters/base.py`, `factory.py` | The core depends only on `ExchangeAdapter` and its four capability interfaces (`reference` / `market_data` / `execution` / `account`), never on a concrete exchange. The factory maps an exchange name to an adapter. |
| Generic WS/REST transport | `hobo/adapters/transport/ws_base.py`, `rest_base.py` | Exchange-agnostic: reconnect/backoff, ping/pong liveness (with its own read-timeout-triggered reconnect), connection lifecycle, dispatch by routing key, and REST signing scaffolding. Extension points are registered callbacks (`.on(key, handler)`, `on_connect`, `on_disconnect`), not methods to override. It knows nothing about OKX, staleness, or risk. |
| OKX adapter | `hobo/adapters/okx/{adapter,rest,websocket,parsing,constants}.py` plus one file per capability: `reference.py`, `market_data.py`, `execution.py`, `account.py` | `websocket.py` holds thin public/private clients on `AbstractWebSocketClient` (only wire format: subscribe/ping/login messages); `parsing.py` is the inbound codec. Each capability file is pure translation. |
| Reference data (specs, startup) | `hobo/adapters/okx/reference.py` | One-time sync REST load of contract spec; maintenance-margin rate is a config constant, not fetched (see `docs/design-notes.md`). |
| Market data (marks + funding) | `hobo/adapters/okx/market_data.py` | `fetch_mark` is a sync REST snapshot used before the stream is up; `stream` subscribes the public WS and publishes `MarkEvent` / `FundingEvent` onto the bus. No state, no staleness, no risk. |
| Execution | `hobo/execution/{base,paper,manager}.py`, `hobo/adapters/okx/execution.py` | `ExecutionClient` (`base.py`) has two implementations - `PaperExecutionClient` and `OkxExecutionClient` - so execution is not exchange-specific; the adapter merely produces the OKX one. `OrderManager` (`manager.py`) fronts whichever client is wired. |
| Account (reconciliation truth) | `hobo/adapters/okx/account.py` | Read-only net positions, balance, and recent fills. `None` for paper. |
| Typed EventBus | `hobo/core/bus.py` | Single typed bus: the adapter publishes market and fill events; subscribers consume them in a defined order (backpressure surfaced as a metric). |
| State: desk -> book -> position | `hobo/risk/model/`, `state.py`, `fold.py`, `math.py` | `model/` is the data shape (`ContractSpec`/`Instrument`, `Order`/`Side`, `RejectReason`/`GateDecision`, `Book`/`Desk`/`Position`/`Limit`); `state.py` holds the state + aggregates; `fold.py` is the event fold (`apply_event`); `math.py` is pure PnL/liquidation/fill-simulation math. |
| Pre-trade gate | `hobo/risk/gate/{engine,checks,context}.py` | Zero-I/O, synchronous. `engine.py` is the `RiskEngine`; `checks.py` are the composable checks; `context.py` builds the shared `OrderContext` once per order. |
| Order gateway (the chokepoint) | `hobo/core/gateway.py` | `OrderGateway.place` is the one place the gate runs in the live flow: mint order -> log `ORDER_REQUEST` -> `RiskEngine.check` -> log `GATE_DECISION` -> submit if approved. Strategy and dashboard orders both go through it. |
| Fill admission + reconcilers | `hobo/core/{fill_ingestor,fill_reconciler,exchange_reconciler}.py` | `FillIngestor` is the single fill-admission gate (dedup by `trade_id`); the two reconcilers are the REST backstops (see `docs/reconciliation.md`). |
| Feed health / kill switch | `hobo/core/health.py`, `hobo/risk/staleness.py` | `FeedHealthMonitor` subscribes to market events to feed a `StalenessWatchdog`; a periodic `check()` trips the desk kill switch on a fresh->stale transition. |
| Composition root | `hobo/builder.py` | `EngineBuilder` wires the whole graph from config + adapter, in `build()` order; shared handles live on the builder only at build time. |
| Lifecycle | `hobo/application.py` | `Application` owns the process lifecycle: periodic fsync/snapshot/staleness tasks, signals, teardown order. `ReplicaApplication` is the standby variant. |
| Strategies | `hobo/strategies/{base,momentum,mean_reversion,oscillator}.py` | Book-agnostic strategy sources. Default wiring runs `OscillatorStrategy` as the book `"scalper"` across the instrument universe; the momentum and mean-reversion classes exist and can be wired as additional books. |
| Append-only log | `hobo/log/{format,writer,reader}.py` | Length-prefixed mmap frames, a 4096-byte header page with `committed_offset`/`last_seq`, growth by `ftruncate`+remap. Log file is `eventlog.bin`. |
| Snapshots | `hobo/log/snapshot.py` | Atomic JSON snapshots (`.tmp` -> fsync -> `os.replace`), retention of the last K. |
| Recovery / reconciliation | `hobo/log/recovery.py` | Snapshot -> replay tail -> reconcile instrument specs, mark freshness, and the book roster (see `docs/reconciliation.md`). |
| Shadow replica | `hobo/replica/tail.py` | Polls `committed_offset`, folds the same events via the same frame parser as the primary's own replay path. |
| Cold path (dashboard, metrics, status) | `hobo/obs/{metrics,server,logging_config}.py` + `hobo/obs/dashboard.html` | Prometheus counters/histograms plus a stdlib `ThreadingHTTPServer` serving `/` (dashboard), `/status`, `/metrics`, `/healthz`, and `POST /order`. |
| Process entrypoint | `hobo/main.py` | Configure logging, load config, build the application graph, run it. Primary vs replica is chosen inside `builder.py` from `replica_mode`. |

## Event flow through the bus

Every mark tick and every order follows the same shape: append to the log first, fold into state, then act.
The wiring is a single typed `EventBus` (`hobo/core/bus.py`); the composition root (`hobo/builder.py`) subscribes handlers in a defined order.

```
adapter market_data().stream(...)   <- MarkEvent / FundingEvent onto the bus
        │
        ▼
Recorder.record                     <- StateStore.record: append MARK_UPDATE / FUNDING_UPDATE, then fold
        │                              (subscribed first, so state is current before anyone reacts)
        ▼
FeedHealthMonitor.on_message        <- feeds the StalenessWatchdog
        │
        ▼
TradeLog.record                     <- (fills only) recent-trade blotter
        │
        ▼
StrategyRunner.handle -> Order | None
        │
        ▼  (if an order was produced)
OrderGateway.place
        │
        ├── StateStore.record OrderRequest   <- append ORDER_REQUEST (audit trail, no-op on state)
        │
        ▼
RiskEngine.check(state, order)      <- zero I/O, reads folded state only
        │
        ├── StateStore.record GateDecisionLog  <- append GATE_DECISION (audit trail, no-op on state)
        │
        ▼ (if approved)
OrderManager.submit -> fill comes back through FillIngestor -> bus
        │
        ▼
Recorder.record                     <- append FILL, then fold (position/PnL updated)
        │
        ▼
cold-path metrics hooks (gate-decision latency, event dispatch)
```

The fill path closes the loop: an approved order submits through `OrderManager`, and the resulting fill (from the execution WS, or the REST backstop) re-enters through the single `FillIngestor` admission gate and back onto the bus, where the `Recorder` logs and folds it exactly like any other event.
Kill-switch and limit-change events follow the same append-then-fold shape but arrive from outside the tick loop (the feed-staleness monitor, or a manual operator action), not from a strategy.

## Why the log comes first, always

Every state mutation in this system - mark update, funding update, fill, limit change, kill switch - is appended to the log *before* it is folded into in-memory state.
That ordering lives in one place: `StateStore.record` (`hobo/core/state_store.py`) appends the event through the `LogWriter`, then calls `apply_event` on the returned envelope, and the recovery path (`hobo/log/recovery.py`) applies the identical fold over the replayed log.
This is what makes replay deterministic: the log is authoritative, and in-memory state is always a pure function of "everything in the log up to some point," never the other way around.
The gate itself never writes anything; it only ever reads the already-folded state.
