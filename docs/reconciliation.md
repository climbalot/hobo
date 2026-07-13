# Reconciliation: what this engine actually does

The exchange is the ultimate source of truth for what we actually hold; the log is the source of truth for our own decision history.
Reconciliation is how those two are kept honest against each other.
Which parts run depends on whether there is an authenticated account: the paper backend (`RISK_ENV=local`) has none, so only the recovery-time public checks apply; the demo backend (`RISK_ENV=demo`) reconciles against a real, authenticated OKX account.

## Recovery-time reconciliation (always, public endpoints)

`hobo/log/recovery.py` runs on every startup, after loading the latest snapshot and replaying the log tail, using only public REST plus the configured seed:

1. **Instrument reference-data drift.**
The contract spec baked into the recovered state is re-fetched from `/api/v5/public/instruments` and compared field by field.
Any difference (a changed tick size, lot size, leverage cap, etc.) replaces the stale spec and logs a `ReconciliationWarning` with kind `INSTRUMENT_SPEC_DRIFT`, recording the old and new value for every field that changed.

2. **Mark-price freshness.**
If the most recently replayed mark for an instrument is older than `MARK_STALENESS_THRESHOLD_NS` (5 seconds, matching the feed staleness watchdog), a fresh mark is fetched from the public mark-price endpoint and seeded into state before the WS feed resumes.
This logs a `ReconciliationWarning` with kind `MARK_STALE`.

3. **Book-roster drift.**
The recovered book roster is reconciled against the configured seed roster (`seeds/desk_seed.json`).
A book newly added to the seed is onboarded fresh (`BOOK_ADDED`); a book that was recovered but is no longer in the seed is *kept and flagged* (`BOOK_NOT_IN_SEED`, with its open positions), never silently discarded, since it may hold an open position.

These warnings are appended to the event log as `RECONCILIATION_WARNING` events (a no-op on the state fold; they exist purely for the audit trail), so a clean recovery with no drift and a fresh mark produces zero warnings.

## Live reconciliation against the demo account (three levels)

When the execution backend is `exchange` and the adapter exposes an authenticated account (`adapter.account()` is not `None`), the builder wires two continuous reconcilers; a third is a manual admin tool.

### 1. Exchange reconciler (position + balance visibility)

`hobo/core/exchange_reconciler.py` polls the exchange's reported net positions and total equity on its own daemon thread (default every 15s), so signed REST calls never touch the asyncio hot path and never fire per dashboard scrape.
It aggregates the engine's own positions across all books, compares them per instrument against the exchange, and publishes the latest `{engine_qty, exchange_qty, diff, matched}` per instrument plus total equity.
That payload is what the dashboard's exchange-reconciliation panel renders.
Divergence - a missed fill, an external/manual trade, or starting on a fresh log against a non-flat account - is exactly what a risk desk must be able to see; this surfaces it without acting on it.

### 2. Fill reconciler (REST backstop for dropped fills)

`hobo/core/fill_reconciler.py` is a REST backstop for fills the private WS dropped.
It runs as an asyncio task (default every 20s), pages the exchange's fills history in an executor thread, and submits any genuinely-missed fill through the **single** `FillIngestor` admission gate.

The admission gate (`hobo/core/fill_ingestor.py`) is the one door every fill passes through, whatever its source (execution WS or this backstop), and its only check today is dedup by exchange `trade_id`: a fill is admitted exactly once, whichever source delivers it first, so the WS and the backstop can never double-count.
The seen-set is seeded from the log at startup, so replayed fills are not re-admitted.

Baseline behavior is the subtle part, and it is deliberate:

- A **continuing** engine baselines just before the log's last real fill (`FillIngestor.last_fill_ns`, minus a slack), so an ungraceful restart *backfills* fills that filled on the exchange during downtime.
That is the whole point of a REST backstop.
- A **fresh** engine (no fills in the log) baselines at wall-clock start instead, so it stays forward-only and does **not** rebuild pre-existing history - which would resurrect stale or closed positions and cannot handle external trades.
Adopting an initial position is the manual net-patch's job, below.

Fills are attributed to a book by parsing the client order id the engine minted; unattributable (external/manual) trades are left alone.
Synthetic net-patch fills carry no `trade_id`, so they are excluded from the baseline and can never be re-adopted by the backstop.

### 3. Manual net-patch (`scripts/reconcile.py`)

`scripts/reconcile.py` is an admin tool to adopt the exchange's current net position as an auditable baseline - for example after starting the engine against an account that is not flat, or to absorb an external/manual trade the fill reconciler deliberately leaves alone.

- **Read-only by default.**
`python scripts/reconcile.py` prints the per-instrument gap between the engine's event-sourced positions and the exchange's actual net positions, and changes nothing.
- **`--apply --book <book>`** appends one reconciliation `FILL` per gapped instrument, attributed to the chosen book at the current mark, so the engine's aggregate matches the exchange on next start.
The patch is preceded by a `POSITION_PATCH` reconciliation warning (the audited intent), and the fill itself is deterministic on replay.

The log is single-writer, so this must be run with the engine **stopped**.

## Where the line to a real order path sits

The demo account exercises the full reconciliation machinery against a live, authenticated venue, but it is still a simulated account with no real capital.
A production order path would keep exactly this three-level shape (continuous position/balance visibility, a fill backstop through one admission gate, and an auditable manual adoption path) and add hard margin isolation and limit reservation on submit - see "Known limitations & path to production" in the README.
