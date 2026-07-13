# Hobo - real-time pre-trade risk engine (OKX perps)

Hobo is a single-process, event-driven pre-trade risk gate for OKX perpetual futures.
Live market data drives a mark-to-market risk state, and every order - from a strategy or entered by hand from the dashboard - must pass a synchronous two-level risk check (per-book and per-desk) before it can reach the exchange.
State is durable via an append-only event log with crash recovery and is reconciled against the exchange.
It runs against a paper backend (offline/CI) and OKX's demo account (real orders + reconciliation); it never touches real capital.

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
```

The hot path runs in one process, one address space - components share memory, with no network or serialization on the order path.
Events flow through a typed `EventBus`; `hobo/builder.py` wires the graph and `hobo/application.py` owns the lifecycle. A warm shadow replica tails the same log and stays ready.

## Design

- **Hot path / cold path.** The per-order check reads in-memory state and does no I/O. Metrics, the dashboard, reconciliation, and logging live on a cold path that never blocks a decision.
- **Two-level gate.** An order is checked against both its book's limits (per-instrument position, notional, drawdown) and the desk aggregate (gross notional, drawdown), short-circuiting on the first reject. Strategy and manual orders go through the same gate.
- **Exchange adapter.** The core depends only on one `ExchangeAdapter` interface with four capabilities - reference, market data, execution, account - so adding an exchange means implementing that interface. OKX lives under `hobo/adapters/okx/`.
- **The Rust boundary.** The gate and state fold are the components I'd rewrite in Rust/C++ for a production order path: GC jitter and no memory-layout control are unacceptable in a hot path measured in microseconds. The rest (feed, reconciliation, cold path, durability) is fine in Python.

## Durability & recovery

Risk state is a materialized view folded from an append-only, memory-mapped event log; the log is the source of truth, and every event carries a monotonic sequence number so replay is deterministic.
fsync is batched (every 100ms) with a two-step commit - the data region is flushed first, then the committed pointer is advanced - so a crash loses at most the uncommitted tail and never claims durability for bytes not on disk.
On restart the engine loads the latest snapshot, replays the log tail, reconciles, and resumes.

## Reconciliation

Against the demo account, reconciliation runs at three levels:

- **Exchange reconciler** - polls net positions and balance and surfaces the engine-vs-exchange gap per instrument on the dashboard.
- **Fill reconciler** - a REST backstop for fills the WS dropped; every fill passes one admission gate (dedup by exchange `trade_id`), baselined off the log's last fill so an ungraceful restart backfills missed fills rather than drifting.
- **Manual net-patch** (`scripts/reconcile.py`) - adopts the exchange's current net position as an auditable baseline.

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                    # add pytest pytest-asyncio ruff for dev

python -m hobo.main                 # RISK_ENV=local (default): paper backend, no keys

cp .env.example .env                # add demo EXCHANGE_API_* keys (demo only, never real)
RISK_ENV=demo python -m hobo.main   # exchange backend against the OKX demo account
```

Dashboard and endpoints at `http://localhost:9090`: `/` (live dashboard - PnL, positions, trade blotter, reconciliation panel, order entry through the real gate), `/status`, `/metrics`, `/healthz`.
