# Design notes

This document records the judgment calls made while building this prototype that are not fully spelled out in the README.
Each one was a real decision point, not an accident, so it's written down here for permanence rather than left implicit in code.

## The WS client is transport-only; the generic base lives in `adapters/transport/`

The generic WS machinery and the OKX-specific client are deliberately kept apart: nothing about generic transport is inherently "a feed," and folding the two into one class would conflate two different concerns.

The generic piece (`AbstractWebSocketClient`: reconnect/backoff, ping/pong liveness with its own read-timeout-triggered reconnect, connection lifecycle, dispatch by routing key) lives in `hobo/adapters/transport/ws_base.py`, exchange-agnostic and with no OKX knowledge at all.
`hobo/adapters/okx/websocket.py`'s public/private clients are thin concrete subclasses that only implement the things that are inherently about wire format: building the subscribe/ping/login messages and handing raw frames to `parsing.py`.

Every extension point on the base class is a registered callback (`.on(key, handler)`, `on_connect`, `on_disconnect`) rather than a method to override.
The OKX `market_data` capability (`hobo/adapters/okx/market_data.py`) registers handlers that translate mark-price/funding-rate frames into domain `MarkEvent` / `FundingEvent` and publishes them onto the `EventBus` - it holds no state and knows nothing about staleness or risk.
Feed health lives one layer up, as a separate bus subscriber (see the staleness note below), so the WS client stays responsible for data transport only, and every domain-specific behavior (what the data means, what to do when it goes stale) is testable independently of any socket.

## Log frame format: length-prefixed, not pure newline-delimited JSON

The log file is `eventlog.bin`, not a `.jsonl`, precisely because the format is not plain newline-delimited JSON.
The actual layout is a fixed 4096-byte header page, then frames of a 4-byte length prefix, the JSON payload, then a cosmetic trailing `\n`.

The reason is crash-safety.
A pure newline-delimited format only detects a torn write if the truncated line happens to fail JSON parsing, which is not guaranteed.
A length-prefixed frame gives an authoritative boundary: the reader knows exactly how many bytes to expect and can validate the trailing sentinel, so a torn write is detected every time, not just when it happens to look invalid.
Because `ftruncate` zero-fills grown space, an unwritten region reads back as a length prefix of 0 - a natural end-of-data sentinel.
See `hobo/log/format.py` for the exact layout.

## Maintenance margin rate is a config constant, not fetched data

OKX's public instruments endpoint (`/api/v5/public/instruments`) returns contract specs (`ctVal`, `ctMult`, `tickSz`, `lotSz`, `lever`) but does not return a maintenance margin rate.
Real MMR figures live behind tiered, position-risk endpoints that require account context.

The maintenance-margin rate is therefore a flat config constant (`[risk] maintenance_margin_rate`, default `0.005`, i.e. 0.5%), read from the per-environment TOML and applied uniformly rather than fetched or tiered by position size.
The split in the model makes this clean: `ContractSpec` is exactly what the adapter returns, and `Instrument` (`hobo/risk/model/instrument.py`) is a `ContractSpec` plus that config-sourced rate, assembled at startup (`Instrument.from_spec`).
This is the same simplification the README already commits to under "tiered maintenance margin" in Known Limitations; it's just made explicit here as *why* it has to be a constant, not only that it is one.

## Liquidation price assumes isolated margin at full stated leverage

There is no per-position margin-mode field driving the risk view, so there is no real signal for how much margin actually backs a given position.

The liquidation formula in `hobo/risk/math.py` assumes isolated margin at the instrument's full stated `max_leverage`:

```
position_margin = notional_at_entry / instrument.max_leverage
```

This is an invented assumption, not something OKX's API told us.
It produces a sane, testable liquidation curve (higher leverage moves liquidation closer to entry, as expected), but it should not be read as "this is what OKX would actually liquidate at" for a real account.

## Fills are instant, full, and slippage-free

The paper backend defines the fill mechanics the offline/CI path runs on.
Every approved order the `PaperExecutionClient` fills does so 100% instantly at the current mark price - no partial fills, no queueing, no slippage model.

This keeps the fill model reusable in one place (`hobo.risk.math.simulate_fill`, shared between the state fold and the gate's own hypothetical-fill preview in `OrderContext`) and keeps strategy behavior deterministic and testable.
A partial-fill or fill-latency model would change the `Fill` event's shape and is a reasonable next step if this were pushed further toward realism, but it's out of scope here.
Note that against the demo account the fills are real (async, from the exchange), so this simplification is scoped to the paper backend only.

## Sustained feed staleness trips the kill switch

OKX's public mark-price and funding-rate channels carry no sequence number, so there is no way to detect a *gap* the way a sequenced feed would.
What is available is staleness: how long since the last tick arrived.

The watchdog in `hobo/risk/staleness.py` is deliberately not just a metric, and deliberately not owned by any WS client.
It is a pure, clock-injectable class (no `asyncio.sleep` inside), so the decision logic is testable without real time passing.
`FeedHealthMonitor` (`hobo/core/health.py`) composes it: it subscribes to `MarkEvent` / `FundingEvent` on the bus to call `record_message()`, and the `Application` drives `check()` from a periodic task (default 1s poll), tripping the desk-level kill switch on a fresh->stale transition and clearing it when ticks resume.
Sustained staleness (default 5 seconds) halts new approvals until fresh ticks return.
A risk gate that keeps silently approving orders against a stale mark is a correctness bug, not merely an ops annoyance, so this defaults to fail-closed rather than fail-open.

## The feed-staleness kill switch is persisted, and cleared on the first live tick after restart

The kill switch is event-sourced state, so a desk killed for "feed stale" is captured in the snapshot and replayed on restart - as it should be, since a crash does not make a dead feed healthy.
But the `StalenessWatchdog` is in-memory and starts fresh every process, with no memory of the last tick, so a recovered-killed desk would never see the fresh->stale-and-back transition that clears the switch.

`FeedHealthMonitor` closes that gap at construction (`hobo/core/health.py`): if the recovered desk is killed specifically for the "feed stale" reason, it seeds the watchdog into the stale state to match.
The first live tick after restart then registers as a stale->fresh transition and clears the kill switch, while a genuinely dead feed keeps it set.
This keeps the persisted safety state honest across restarts without leaving a healthy desk stuck killed.

## The two-level gate: desk aggregate vs. the sum of book limits

The gate is two-level on purpose: an order from a book is checked against **both** its own limits and the desk aggregate (`hobo/risk/gate/checks.py`, run in order by the `RiskEngine`).
The interesting case, and the one that justifies the hierarchy, is a book that is fully within its own limits whose order would still push the *desk* over its aggregate cap - the gate rejects it.

The shipped desk gross-notional limit (`700,000` in `seeds/desk_seed.json`) is deliberately set below the unconstrained sum the books could reach, so this desk-level rejection is a real, reachable behavior rather than something that only shows up in unit tests with bespoke fixtures.
The default roster runs a single strategy book (`"scalper"`) across several instruments, but the structure generalizes to N books: the desk aggregates all of them, and adding a second or third book (the momentum and mean-reversion strategies already exist) is a seed-roster change, not a code change.
The point of the two-level design is that desk-level protection does not depend on how many books are trading or on any one book being individually over its own limit.

## Reconciliation is real against the demo account

Earlier revisions of this prototype had no account access, so recovery-time "reconciliation" was narrowed to public-endpoint checks only.
That is no longer true: run against OKX's demo (simulated) account (`RISK_ENV=demo`), the engine reconciles against a real, authenticated account at three levels - a continuous exchange reconciler that surfaces the engine-vs-exchange position gap on the dashboard, a fill reconciler that backstops WS-dropped fills through the single admission gate, and a manual net-patch admin tool.
The public-endpoint checks (instrument-spec drift, mark freshness) still run on every recovery regardless of backend.
See `docs/reconciliation.md` for the full writeup; the paper backend (`RISK_ENV=local`) has no account, so only the public-endpoint recovery checks apply there.
