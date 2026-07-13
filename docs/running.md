# Running this project

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .            # runtime deps; add: pip install pytest pytest-asyncio ruff  for dev
```

Configuration is split by design (`hobo/config.py`):

- Non-secret, per-environment settings live in committed TOML under `config/`, selected by `RISK_ENV` (default `local`).
- Secrets (API credentials) and a few runtime knobs (`METRICS_PORT`, `REPLICA_MODE`, `DATA_DIR`, `APP_HOME`) come from env vars / `.env` only, never the TOML, so the config files stay safe to commit.

There is no Docker: this runs as a plain Python process.

## Local (paper, no keys) - the default

```bash
python -m hobo.main            # RISK_ENV=local
```

`local` (`config/local.toml`) uses the paper execution backend, so it needs no API keys.
Marks and funding still stream live from OKX's public WS feed; only fills are simulated (instant, full, at the mark - see `docs/design-notes.md`).
On startup the process logs its recovery line (`recovered state: from_snapshot=... replayed=... warnings=...`), connects to the feed, and starts trading the default `scalper` book.

## Demo (real orders against the OKX demo account)

```bash
cp .env.example .env           # add demo EXCHANGE_API_KEY / _SECRET / _PASSPHRASE (demo only, never real)
RISK_ENV=demo python -m hobo.main
```

`demo` (`config/demo.toml`) uses the exchange execution backend against OKX's demo (simulated) venue: approved orders route to the exchange over its authenticated private WS, and fills return async.
This also enables the live reconcilers (see `docs/reconciliation.md`), since there is now a real account to reconcile against.
`execution.backend=exchange` requires the three `EXCHANGE_API_*` env vars; startup fails fast if they are missing.
The demo *public* feed uses `wspap.okx.com` (live marks); the private/account channels use the EEA endpoint the demo account's auth is bound to.

Each environment keeps its own data directory (`data/local/`, `data/demo/`), so paper and demo state (event log + snapshots) never mix.

## Dashboard and observability

The cold path serves everything over one plain HTTP port (`METRICS_PORT`, default 9090) on its own daemon thread, so it never touches the asyncio hot path and only ever reads state:

- `http://localhost:9090/` - a live HTML dashboard: per-book PnL attribution, positions, a trade blotter, the exchange-reconciliation panel (demo only), and an order-entry form.
Orders entered here are placed through the **same** `OrderGateway` as strategy orders, so hand-entered and strategy orders are gated identically.
- `http://localhost:9090/status` - full JSON state dump plus a computed PnL/position summary.
- `http://localhost:9090/metrics` - Prometheus text format.
- `http://localhost:9090/healthz` - liveness check.

## Graceful shutdown

Stop with `Ctrl-C` (SIGINT) or SIGTERM.
Shutdown is graceful and ordered (`hobo/application.py`): it stops the adapter/feed, cancels the background tasks, writes a final snapshot, then commits and closes the log before exiting.

## Warm replica

A shadow replica tails the primary's event log over a shared data directory, folds the same events through the same frame parser, and stays warm with its own `/status`.
It does **not** open its own OKX connection - it purely reads the log - so it must point at the same data directory as the primary (same `RISK_ENV`, or the same `DATA_DIR`).

Run one alongside a primary by setting `REPLICA_MODE=true` (equivalently `[replica] mode = true` in the TOML) and a distinct port:

```bash
# primary (e.g. demo), in one terminal
RISK_ENV=demo python -m hobo.main

# warm replica against the same data/demo/ log, in another terminal
RISK_ENV=demo REPLICA_MODE=true METRICS_PORT=9091 python -m hobo.main
```

The replica waits for the primary to create the log file, then tails `committed_offset` and folds each newly committed range.
It only ever trusts `committed_offset`, never scan-ahead, so it can never get ahead of what a primary restart would also see as durable.
`curl :9091/status` should converge with `:9090/status` within a poll interval.

## Manual replica promotion

Automated failover promotion is out of scope (see "High availability & consensus" in the README) - the replica stays warm, but promoting it to primary is a manual runbook, not code:

1. Confirm the primary is actually down.
Do not promote a replica against a live primary - that produces two writers on one log, which the format has no protocol for reconciling (the log is single-writer).
2. Stop the replica process.
3. Restart it as a primary against the **same** data directory: same `RISK_ENV` (or `DATA_DIR`), with `REPLICA_MODE` unset/false.
The recovery flow runs identically regardless of which role a process last held, so it loads the latest snapshot, replays the log tail, reconciles, and resumes - now also opening a real OKX connection and accepting new ticks.
4. Point any downstream consumers (order sources, dashboards) at the promoted instance's address.
5. Bring up a fresh warm replica against the promoted primary once it's stable.
