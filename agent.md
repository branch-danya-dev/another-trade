# agent.md

## Project mission

Build a new trading system from scratch for exactly one deterministic strategy:

**intraday trend-following breakout → retest of objective previous-period levels on Bybit USDT perpetuals.**

Do not reuse strategy logic, heuristics, risk gates, scoring systems, ML layers, level detectors, or architectural assumptions from any previous trading-bot project unless this repository later documents an explicit decision to do so.

The current canonical strategy is:

- [docs/SPEC_V0.2.md](docs/SPEC_V0.2.md)
- [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md)
- [docs/VALIDATION_PROTOCOL.md](docs/VALIDATION_PROTOCOL.md)

If code and documentation disagree, the frozen specification wins until the specification is intentionally versioned.

---

## Current phase

Current phase:

```text
DATA AUDIT
```

Do not jump directly to a live bot.

Expected order:

1. data-audit tooling;
2. immutable historical dataset/manifest;
3. event-driven backtester;
4. development-period verification;
5. frozen validation;
6. final holdout;
7. forward/demo runtime;
8. minimal-size real runtime only after the validation protocol permits it.

---

## Strategy-change rule

Never modify trading behavior merely to make a test pass.

Any change to:

- level definitions;
- allowed directions;
- EMA/trend rules;
- breakout definition;
- volume filter;
- ATR stop;
- cost floor;
- room filter;
- sessions;
- news behavior;
- risk sizing;
- capacity;
- daily limits;
- fill semantics;
- rounding;
- intrabar priority;
- TP/BE/runner logic;
- validation criteria;

requires a new documented specification version before implementation.

Bug fixes that restore already-documented behavior do not require a strategy version bump, but they require a regression test.

---

## Holdout protection

Treat 2025 and 2026 partitions exactly as defined in the validation protocol.

Do not inspect final-holdout PnL, trades, signal counts, symbol results, or parameter comparisons before the documented unlock gate.

Do not add temporary scripts that print holdout results "just for debugging."

If debugging requires market data from a holdout period, use structural/data-integrity checks that cannot reveal strategy outcome where possible, or use development-period fixtures.

---

## Architecture requirement

Backtest, demo, and live modes must share the same pure strategy decision core.

Preferred dependency direction:

```text
domain / strategy core
        ↑
application orchestration
        ↑
adapters: historical data / Bybit / persistence / alerts
```

The strategy core must not know whether events came from:

- historical replay;
- demo WebSocket/REST;
- live WebSocket/REST.

Mode-specific code may implement execution mechanics, data transport, persistence, and reconciliation, but not redefine strategy rules.

---

## Determinism

Given:

- the same spec version;
- same execution-model version;
- same data snapshot;
- same starting equity;
- same configuration;
- same event order;

the backtester must produce byte-for-byte equivalent normalized trade decisions and deterministic result metrics, excluding intentionally nondeterministic metadata such as wall-clock runtime.

Use explicit deterministic tie-breaks everywhere.

Never depend on dictionary/set iteration order for trading decisions.

---

## Numeric rules

Use Decimal or an equivalent exact decimal representation for:

- prices;
- tick sizes;
- quantity steps;
- fees;
- funding rates where practical;
- order quantities;
- realized PnL.

Do not use binary float for exchange-grid rounding.

All price/quantity rounding must go through one shared utility used by backtest and live.

The rounding behavior is defined in SPEC_V0.2 and must have unit tests for LONG and SHORT cases.

---

## Time rules

Store timestamps in UTC.

Use timezone-aware datetime objects.

Use IANA zones:

- Europe/Amsterdam;
- America/New_York;
- UTC.

Never hard-code DST offsets.

Do not use an unfinished 1H candle for trend context.

At every decision timestamp, prove that every input candle has closed.

---

## Data rules

Never silently:

- forward-fill missing execution prices;
- use today's universe for historical dates;
- remove delisted symbols;
- replace unknown historical tick/qty metadata with current values;
- synthesize funding events;
- infer news timestamps from a generic recurring schedule when official historical timestamps exist.

Every fallback must be explicit, versioned, and visible in audit output.

Data quality limitations are findings, not errors to hide.

---

## First implementation target

Implement the data-audit CLI before the backtester.

It should, at minimum:

1. enumerate Bybit linear USDT perpetuals in Trading and Closed states;
2. persist launch/delivery metadata;
3. test historical 1m kline coverage over instrument lifetime;
4. inspect gap structure;
5. test funding-history coverage;
6. report historical grid metadata status;
7. normalize CPI, Employment Situation, and FOMC event timestamps from official sources;
8. produce the artifacts listed in DATA_CONTRACT.md;
9. produce an immutable manifest/hash for the audited dataset.

The audit should support resumable downloads and local caching. Do not repeatedly hit APIs for already-verified immutable historical pages.

---

## Backtester requirements

When the project reaches the backtest phase:

- use an event-driven simulation;
- canonical raw bar resolution is 1m;
- derive 15m/1H/D/W bars deterministically;
- strategy decisions occur only after bar close;
- pending orders exist as explicit state;
- reservations exist before fills;
- live-like order lifecycle states are represented;
- fill rules match SPEC_V0.2;
- ambiguous OHLC order is pessimistic;
- fees, slippage, and funding are first-class ledger entries;
- every rejected setup stores a machine-readable reason.

Do not build a vectorized backtest that cannot represent pending LIMIT lifetime, intrabar priority, capacity reservations, partial lifecycle, and news cancellation semantics.

---

## Expected reason codes

Prefer stable reason codes rather than prose-only logging.

Examples:

```text
REJECT_WRONG_LEVEL_DIRECTION
REJECT_LEVEL_CONSUMED
REJECT_TREND_ALT
REJECT_TREND_BTC
REJECT_VOLUME
REJECT_SESSION
REJECT_NEWS
REJECT_COST_FLOOR
REJECT_ROOM
REJECT_SLOT_CAPACITY
REJECT_RISK_CAPACITY
REJECT_DAILY_TRADE_CAPACITY
REJECT_DAILY_LOSS_STOP
REJECT_NOTIONAL_MINIMUM
REJECT_DATA_GAP
REJECT_GRID_UNKNOWN

ENTRY_EXPIRED
ENTRY_CANCELLED_NEWS
ENTRY_CANCELLED_DAILY_STOP
ENTRY_FILLED

EXIT_INITIAL_STOP
EXIT_TP1
EXIT_BE
EXIT_TP2
EXIT_FALSE_BREAK
EXIT_SESSION_END
```

Keep reason codes backward-compatible within one spec version.

---

## Testing requirements

Before any validation run, tests must cover at least:

### Levels

- PDH/PDL UTC day boundaries;
- PWH/PWL Monday UTC boundaries;
- allowed direction mapping;
- one-tick duplicate merge;
- multi-level breakout outermost selection;
- crossed-level consumption.

### Closed-candle protection

- 10:45 decision cannot use 10:00–11:00 unfinished 1H;
- EMA calculation excludes unfinished bars;
- breakout volume average excludes breakout candle.

### Grid

- LONG/SHORT entry rounding;
- LONG/SHORT stop rounding;
- TP1 rounding away from entry;
- TP2 objective rounding toward position;
- cost-adjusted BE rounding;
- quantity floor to qtyStep.

### Execution

- touch does not fill;
- strict penetration fills;
- entry + TP in same minute does not credit TP;
- entry + stop in same minute stops out;
- stop + TP in same existing-position minute resolves to stop;
- TP1 + BE same minute resolves TP1 then BE;
- false-break only acts after confirmed 15m close.

### Capacity

- pending order reserves slot;
- pending order reserves risk;
- pending order reserves daily fill capacity;
- no fourth daily filled setup is possible;
- third fill cancels stray pending entries defensively.

### News

- blackout cancels unfilled entries;
- consumed level remains consumed;
- open position remains managed;
- Eastern-time DST converts correctly.

### Data integrity

- delisted instrument appears in historical universe during lifetime;
- future listing is absent before launch;
- unresolved 1m gap invalidates affected setup;
- funding applied only at actual event timestamp.

---

## Live-runtime requirements

When live/demo execution is eventually implemented:

1. stop protection must be placed on exchange immediately after fill;
2. partial fills must resize protection to actual filled quantity;
3. reconnect must reconcile exchange positions and orders before new decisions;
4. race conditions such as fill-during-cancel must be idempotent;
5. orderLinkId/idempotency keys must prevent duplicate submissions;
6. API key must have no withdrawal permission;
7. secrets must never enter git;
8. production keys should be IP-restricted;
9. global maximum quantity/notional sanity checks must exist independently of strategy sizing;
10. emergency kill switch must cancel entries and prevent new risk;
11. alerts must report reconciliation failures, unprotected exposure, rejected stops, and unexpected exchange state.

A process crash must not leave an open live position without exchange-side protection.

---

## Code quality

Keep modules small and domain-oriented.

Prefer typed models for:

- candles;
- levels;
- signals;
- setup state;
- orders;
- fills;
- positions;
- reservations;
- funding events;
- news events;
- ledger entries;
- rejection reasons.

Validate external API payloads at adapter boundaries.

Do not pass raw Bybit dictionaries deep into the strategy core.

Persist enough state to reproduce every decision.

---

## Documentation discipline

When a meaningful implementation decision is made:

- update the relevant document in the same change;
- do not leave critical behavior only in comments or chat history.

README is orientation, not the canonical rulebook.

Canonical behavior belongs in versioned docs.

---

## Definition of done for the next milestone

The Data Audit milestone is complete when:

- the audit runs end-to-end;
- Trading and Closed inventory is captured;
- 1m/funding coverage report exists;
- data gaps are quantified;
- historical grid status is quantified;
- news calendar is normalized;
- survivorship limitations are explicit;
- dataset manifest is hashed;
- results are reproducible from a clean environment;
- tests cover pagination, time boundaries, gap detection, and resume/cache behavior.

Only then proceed to implementing the backtester.
