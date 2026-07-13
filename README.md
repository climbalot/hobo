# Hobo - real-time pre-trade risk engine (OKX perps)

Hobo is a single-process, event-driven **pre-trade risk gate** for crypto perpetual futures.
Live OKX market data drives a mark-to-market risk state; every order - whether from a strategy or entered by hand from the dashboard - must pass a synchronous, two-level risk check (per-book and per-desk) before it can reach the exchange.
State is durable via an append-only event log with crash recovery, reconciled against the exchange, and replicated to a warm standby.

---

## What this is (and what it isn't)

This is a **focused prototype**, built to work through the streaming, low-latency version of pre-trade risk aggregation and to make the design decisions behind it explicit and defensible.
It runs end-to-end against OKX: a paper backend for offline/CI, and OKX's **demo (simulated) account** for real order placement and reconciliation against a live exchange.

It is **not** a production trading system, and it does not touch real capital - only paper fills and the OKX demo account.
The value is the architecture and the reasoning: where latency actually matters, how risk state stays correct across crashes and dropped fills, and where the line to production sits (including a rewrite of the hot path in a systems language - see [the Rust boundary](#language-choice-latency-and-the-rust-boundary)).

Context: I've run production risk aggregation at scale in a prior role; that system was polling-based (REST -> store -> aggregate), a reasonable design for multi-exchange aggregation but not low-latency.
Hobo is the streaming variant, built to reason about the tradeoffs firsthand.

---

## Terminology (read this first)

Two terms are easy to confuse, so they are kept strictly distinct:

- **Book** - a *strategy-level trading unit* with its own risk limits. A "book" is a strategy. In code: `book`, `book_id`.
- **Order book** - exchange market-data depth (the bid/ask ladder). Always written in full. Hobo consumes mark price and funding, not full depth.

The hierarchy is **desk -> book -> position**.
A desk aggregates its books; a book holds one position per instrument it trades.

---

## Architecture

```
     OKX adapter (one interface, four capabilities)
     reference (REST) · market data (WS+REST) · execution (WS) · account (REST)
                                  │
        marks / funding ─────────┼───────── fills (WS) + REST backstop
                                  ▼
     ┌──────────────────────────────────────────────────────────┐
     │                  Hot path (one process)                    │
     │   EventBus ─▶ fold ─▶ State: desk → book → position(s)     │
     │                         mark-to-market PnL · exposure       │
     │                                    ▲                        │
     │   strategies / UI order ─▶ Pre-trade gate ──(reads state)──┘│
     │                            book limits + desk aggregate      │
     │                            approve → execution · reject      │
     └──────────────────────────────────────────────────────────┘
              │                    │                      │
              ▼                    ▼                      ▼
     ┌────────────────┐   ┌────────────────┐    ┌──────────────────┐
     │ Append-only log│   │ Reconcilers    │    │ Cold path         │
     │ mmap·seq·snap  │   │ positions·fills│    │ metrics·dashboard │
     └────────────────┘   └────────────────┘    └──────────────────┘
              │
              ▼
       ┌────────────────┐
       │ Shadow replica │  tails the log, folds the same events, stays warm
       └────────────────┘
```

Everything on the hot path runs in **one process, one address space**.
Components call each other in-process, with no network or serialization on the order path - the hot path is a set of components sharing memory, not a set of services.
The log write, the reconcilers, the cold path, and the replica are the only things off the critical decision path.

Events flow through a single typed **EventBus** (`hobo/core/bus.py`): the adapter publishes market and fill events, subscribers (the state fold, feed-health monitor, trade blotter, strategy runner) consume them in a defined order.
The composition root (`hobo/builder.py`) wires the whole graph; `hobo/application.py` owns the lifecycle (periodic fsync/snapshot/staleness tasks, signals, startup/shutdown).

---

## The exchange adapter

The engine core depends only on **one interface** (`hobo/adapters/base.py`), never on a concrete exchange.
An `ExchangeAdapter` is a factory of four small, single-purpose capabilities plus the lifecycle of its own connections:

- **`reference()`** - instrument contract specs (multiplier, tick/lot size, settlement ccy). Sync REST, loaded at startup.
- **`market_data()`** - marks and funding: a synchronous REST snapshot (`fetch_mark`, used before the stream is up) and a live WS `stream` that publishes onto the bus.
- **`execution()`** - approved orders out, fills back, over the private WS.
- **`account()`** - read-only account truth (net positions, balance, recent fills), for reconciliation. `None` for paper.

Adding an exchange means implementing these four capability interfaces plus the adapter, and registering it in `hobo/adapters/factory.py` - nothing in the core changes.
The OKX implementation lives under `hobo/adapters/okx/`, split by concern: `rest`, `websocket`, `parsing` (inbound codec), `constants`, and one file per capability (`reference`, `market_data`, `execution`, `account`).
Generic, exchange-agnostic WS/REST transport (reconnect/backoff, ping/pong, signing scaffolding) lives in `hobo/adapters/transport/`.

Execution is separate from the adapter on purpose: `ExecutionClient` (`hobo/execution/base.py`) has two implementations - the in-process `PaperExecutionClient` and the real `OkxExecutionClient` - so it is not exchange-specific and lives in the execution layer, which the adapter merely produces.

---

## Core design decisions

### Hot path / cold path split

The per-order risk check reads local in-memory state and does no I/O.
Everything latency-insensitive - metrics, the dashboard, reconciliation, alerting - lives on a cold path that reads state or consumes the event stream but never blocks a decision.
This split is the central commitment; most other decisions follow from protecting the hot path.

### The pre-trade gate is two-level

An order from a book is checked against **both** its own limits and the desk aggregate.
The gate (`hobo/risk/gate/`) is an ordered pipeline of composable, zero-I/O checks that short-circuits on the first reject:

1. **Order-level** - kill switch, fat-finger (implausibly large single order).
2. **Book-level** - per-instrument position limit, book-aggregate notional, book drawdown.
3. **Desk-level** - desk gross notional across all books, desk drawdown.

The interesting case - and the one that justifies the hierarchy - is a book *within its own limits* whose order would push the *desk* over its aggregate cap: the gate rejects it.
The order's hypothetical post-fill effect is computed **once** into an `OrderContext` (`hobo/risk/gate/context.py`) and shared by every check, so no check recomputes.
Limits are modeled as a `Limit` enum (`NOTIONAL` / `DRAWDOWN` / `POSITION`) with each scope holding only the limits that apply to it, so a new limit is a new enum member rather than a schema change.

These checks are here because they **must** block *before* the order goes out - a limit breach or an active kill switch can't be corrected after the fill.
That is the justification for them being synchronous and in the hot path; anything evaluable after the fill belongs on the cold path.
Manual orders from the dashboard go through the **same** `OrderGateway.place`, so hand-entered and strategy orders are gated identically - the gate is the single chokepoint (`hobo/core/gateway.py`).

### Language choice, latency, and the Rust boundary

This prototype is Python.
A Python pre-trade check runs in the tens-of-microseconds range with GC jitter on the tail - not single-digit microseconds.
The **pre-trade gate and the state fold are exactly the components I'd rewrite in Rust or C++** for a production order path: GC pauses put non-deterministic spikes on p99, and Python gives no control over memory layout, both unacceptable in a hot path measured in microseconds.
The rest of the system (feed handling, reconciliation, cold path, durability) is fine in Python.
Knowing precisely *where* the language boundary sits, and why, is the point.

---

## Instrument & risk model

**Scope: OKX linear (USDT-margined) perpetuals**, several instruments at once (BTC / ETH / SOL by default).
Linear keeps the PnL math in USDT and clean; inverse (coin-margined) perps invert the formula and are noted as an extension below.

The model splits the exchange contract from the risk view of it (`hobo/risk/model/`):

- **`ContractSpec`** - the pure exchange contract (multiplier, tick/lot/min size, max leverage, settlement ccy), exactly what the adapter returns. No risk parameters.
- **`Instrument`** - a `ContractSpec` plus the maintenance-margin rate, assembled at startup from config. The risk core takes `Instrument`.

State is `desk -> book -> position`, where a book holds one `Position` per instrument it trades.
Per-instrument position limits, book-aggregate notional/drawdown, and desk-aggregate notional/drawdown are enforced together.
Mark price drives mark-to-market PnL, exposure, and liquidation distance on every tick; funding is applied on schedule so it affects PnL over the session.
**Liquidation distance** falls out of position, entry, margin, and the maintenance-margin rate - only meaningful with a live, moving mark, which is the core reason the feed exists.

---

## Durability & recovery

Risk state is a **materialized view** folded from an event stream; the source of truth is an **append-only log on disk** (`hobo/log/`).

### The event log

- Every state-mutating event (fill, mark update, funding, limit change, kill-switch toggle) is appended with a **monotonic sequence number**.
- The file is **memory-mapped**; appends are memory writes the OS flushes - the same mechanical design as a log-based messaging system, simplified.
- Sequence numbers make replay deterministic and let the replica detect gaps.

### fsync is a decision, not a default

fsync-per-event is durable but adds syscall latency; fsync-on-interval is fast but risks losing the log tail on power loss.
For a *risk* system the nuance is that the pre-trade check reads memory and never waits on fsync, and on restart we rebuild from the log *and* reconcile against the exchange - so losing the last few milliseconds is recoverable.
**Current setting: batched fsync every 100ms**, driven by a periodic task in `hobo/application.py`.
Commit is two ordered fsyncs: the data region is flushed first, *then* the header's committed pointer is advanced and flushed, so a crash between the two loses at most the uncommitted tail and never claims durability for bytes not on disk (`hobo/log/writer.py`).

### Recovery and reconciliation

```
crash → restart → load latest snapshot
                → replay log events after the snapshot
                → reconcile: instrument specs, mark freshness, and (with an account) positions/fills
                → resume
```

The exchange is the ultimate source of truth for what we actually hold; the log is the source of truth for our own decision history.
Reconciliation is real when running against the demo account, and runs at three levels:

- **Exchange reconciler** (`hobo/core/exchange_reconciler.py`) - polls net positions and balance, and surfaces the engine-vs-exchange gap per instrument on the dashboard.
- **Fill reconciler** (`hobo/core/fill_reconciler.py`) - a REST backstop for fills the private WS dropped. Every fill from any source passes one admission gate (`FillIngestor`, dedup by exchange `trade_id`), so the WS and the backstop can't double-count. It baselines off the **log's last fill**, so an ungraceful restart backfills fills missed during downtime rather than silently drifting; a fresh log stays forward-only so it doesn't resurrect closed history.
- **Manual net-patch** (`scripts/reconcile.py`) - an admin tool to adopt the exchange's current net position as a baseline (e.g. after starting against a non-flat account). Read-only by default; `--apply` writes an auditable, deterministic reconciliation fill.

Snapshots of the folded state bound both replay time and disk growth; segment rotation and cold-tiering are noted as production evolution below.

---

## High availability & consensus

This section is a design position, independent of how much is implemented.

### Consensus stays *out* of the order path - deliberately

The instinct for "HA + consistent state" is to reach for a consensus protocol (Raft / Aeron Cluster).
For a **pre-trade gate that would be a mistake in the hot path**: consensus means cross-machine round-trips, hundreds of microseconds to milliseconds, and if every order waited for a quorum the low-latency premise is gone.
So the design splits by latency sensitivity:

- **Per-order risk checks** read **local in-memory state** - fast, no network, no quorum.
- **State changes** are appended to an **ordered event log**; a **hot standby** applies the same log deterministically and stays ready.
- **Consensus is reserved for what must be globally consistent but isn't per-order-latency-sensitive** - kill-switch state, limit changes, configuration.

### Where Aeron / Raft fits, and the prototype

Production would use a replicated log (Aeron Cluster, or Raft) for the ordered event log and the globally-consistent control state.
Aeron's design - a memory-mapped log in shared memory - is the same primitive this prototype uses locally, which is why the local-log approach maps cleanly onto it.
A shadow replica process (`hobo/replica/tail.py`) tails the same log over a shared volume, folds the same events through the same frame parser, and stays warm with its own `/status` for lag.
It does not connect to OKX independently.
**Automated failover promotion is not built** - promoting a replica without risking two writers on one log is a manual runbook, a deliberate scope line.

---

## Latency

The characteristic metric is **tick-to-decision**: how fast a market-data update propagates into risk state and a gate decision.
The gate and fold are synchronous, zero-I/O, in-memory reads/writes, so the per-order check is on the order of tens of microseconds in Python - dominated on the tail by GC, which is exactly the argument for the [Rust boundary](#language-choice-latency-and-the-rust-boundary).
The honest framing: this isolates gate/fold cost from network and OS-scheduling noise, so it is a floor, not what tick-to-decision looks like under real WS jitter and sustained load.

---

## Running it

```bash
# one-time setup
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # runtime deps; add pytest pytest-asyncio ruff for dev

# offline / paper (no keys needed) - the default env
python -m hobo.main                    # RISK_ENV=local

# against the OKX demo (simulated) account - real orders + reconciliation
cp .env.example .env                   # add demo EXCHANGE_API_* keys (demo only, never real)
RISK_ENV=demo python -m hobo.main
```

Configuration is split: non-secret per-environment settings live in committed TOML (`config/local.toml`, `config/demo.toml`), selected by `RISK_ENV` (default `local`); secrets and a few runtime overrides come from `.env` / env vars only (`hobo/config.py`).
`local` uses the paper execution backend; `demo` uses the exchange backend against OKX's demo endpoints.
The demo *public* feed uses `wspap.okx.com` (live marks); the private/account channels use the EEA endpoint the demo account's auth is bound to.
Each environment keeps its own data directory (`data/local/`, `data/demo/`) so paper and demo state never mix.

**Dashboard and observability** (cold path, `http://localhost:9090`):

- `/` - a live dashboard: per-book PnL attribution, positions, a trade blotter, the exchange-reconciliation panel, and an order-entry form that places orders through the real gate.
- `/status` - JSON state snapshot. `/metrics` - Prometheus. `/healthz` - liveness.

---

## Known limitations & path to production

Explicitly scoped out, with the production direction:

- **Systems-language hot path** - the gate and fold would move to Rust/C++ for deterministic p99 (no GC) and memory-layout control. The single biggest gap for a real order path.
- **Consensus for control state** - kill switch / limits / config would move behind Raft or Aeron Cluster; the per-order path stays local and unchanged.
- **Automated failover** - the warm replica exists; safe promotion is a manual runbook, not automated.
- **Spot & inverse instruments** - the `ContractSpec` / `Instrument` split is designed to accommodate more instrument types; only linear perps are implemented (spot has no margin/funding/liquidation; inverse inverts the PnL formula).
- **Tiered maintenance margin** - real venues ladder the maintenance rate with position size; this uses a single flat rate.
- **Full order-book depth** - only mark / top-of-book is consumed; risk doesn't need L2.
- **Limit reservation** - with async fills there is a window where two in-flight orders both pass because neither has filled; a production gate reserves limit on submit.
- **Account model** - books are logical risk units against one account (netting reconciliation). Production would weigh sub-account-per-book (hard margin isolation) against logical tagging.
- **Log segmentation & cold-tiering** - snapshots are built; segment rotation and archival are the next durability step.

---

## Repo layout

```
hobo/
  main.py            process entrypoint: configure logging, load config, build, run
  builder.py         composition root - wires the whole graph from config + adapter
  application.py     lifecycle: periodic fsync/snapshot/staleness tasks, signals, replica
  config.py          per-environment TOML + env-var secrets, typed sub-objects
  adapters/
    base.py          ExchangeAdapter + capability interfaces (reference/market_data/execution/account)
    factory.py       exchange name -> adapter
    transport/       generic WS/REST base clients (reconnect, ping/pong, signing)
    okx/             OKX impl, split: rest · websocket · parsing · constants · reference · market_data · execution · account · adapter
  core/
    bus.py           typed EventBus (single-consumer FIFO, backpressure metric)
    gateway.py       OrderGateway - the one place the pre-trade gate runs
    order_bridge.py  dashboard order entry, bridged onto the engine loop
    fill_ingestor.py single fill-admission gate (dedup by trade_id)
    fill_reconciler.py / exchange_reconciler.py   REST reconciliation
    health.py        feed-staleness -> desk kill switch
    recorder.py · state_store.py · strategy_runner.py · trade_log.py
  execution/         ExecutionClient (paper + exchange) + OrderManager
  risk/
    model/           ContractSpec/Instrument · Order/Side · RejectReason/GateDecision · Book/Desk/Position/Limit
    gate/            engine (RiskEngine) · checks · context (OrderContext)
    math.py          pure PnL / liquidation / fill-simulation math
    state.py         the state (desk→book→position) + aggregates
    fold.py          the event fold (apply_event)
    staleness.py     clock-injectable staleness watchdog
  strategies/        strategy sources (base · momentum · mean_reversion · oscillator)
  log/               append-only mmap log: format · writer · reader · snapshot · recovery · events
  replica/           shadow standby (tails committed_offset, folds the same events)
  obs/               cold path: metrics · stdlib HTTP server + dashboard.html · logging
config/              local.toml · demo.toml (per-environment, non-secret)
seeds/               desk/book roster + risk limits (domain data)
scripts/reconcile.py admin net-patch tool
tests/               pytest suite
docs/                architecture · design-notes · reconciliation · running
```
