# Data Contract v0.1.1

Status: **REQUIRED BEFORE BACKTEST IMPLEMENTATION**

This document defines which external facts the project is allowed to trust, how historical coverage is audited, and which missing data block a claim of valid backtest parity.

The canonical trading rules are defined in [SPEC_V0.2.1.md](SPEC_V0.2.1.md). This document must not change strategy behavior.

---

## 1. Canonical sources

### Bybit instrument inventory

Primary endpoint:

```text
GET /v5/market/instruments-info
category=linear
```

Required inventories:

- Trading instruments;
- Closed instruments.

The audit must filter the crypto-perpetual universe explicitly rather than trusting `category=linear` alone. Eligible metadata must satisfy the strategy contract:

```text
contractType = LinearPerpetual
quoteCoin = USDT
settleCoin = USDT (where present)
isPreListing = false
marketRegion = "" / not applicable
```

TradFi perpetuals, pre-market contracts, delivery futures, event contracts, and other non-crypto linear instruments are excluded.

Fields to persist where available:

- symbol;
- status;
- contractType;
- baseCoin;
- quoteCoin;
- settleCoin;
- launchTime;
- deliveryTime;
- priceFilter.tickSize;
- lotSizeFilter.qtyStep;
- lotSizeFilter.minOrderQty;
- lotSizeFilter.minNotionalValue;
- fundingInterval.

Reference:
https://bybit-exchange.github.io/docs/v5/market/instrument

Important limitation: the current endpoint describes current instrument metadata. It must not be assumed that current tickSize, qtyStep, or minimums were valid throughout the instrument's full historical lifetime.

### Bybit historical last-price klines

Primary endpoint:

```text
GET /v5/market/kline
category=linear
interval=1
```

Reference:
https://bybit-exchange.github.io/docs/v5/market/kline

The canonical market-price history for the backtester is 1m OHLCV/turnover.

15m, 1H, UTC daily, and UTC weekly bars used by the strategy must be derived from the canonical 1m dataset rather than mixing independent higher-timeframe downloads into strategy calculations.

This gives one timestamp convention and one auditable aggregation path.

### Bybit historical mark-price klines

Primary endpoint:

```text
GET /v5/market/mark-price-kline
category=linear
interval=1
```

Reference:
https://bybit-exchange.github.io/docs/v5/market/mark-kline

Mark-price history is required around every funding event because Bybit computes USDT-perpetual funding from position value based on Mark Price.

Persist 1m mark-price OHLC. The exact convention for the funding timestamp is:

- primary value: OPEN of the 1m Mark Price candle with `startTime == fundingRateTimestamp`;
- fallback: CLOSE of the immediately preceding complete 1m Mark Price candle, flagged `MARK_PRICE_1M_APPROX`;
- if neither exists, affected trades cannot be parity-quality.

The audit must quantify how often fallback approximation is used and must test this convention on known funding timestamps.

### Bybit funding history

Primary endpoint:

```text
GET /v5/market/funding/history
category=linear
```

Reference:
https://bybit-exchange.github.io/docs/v5/market/history-fund-rate

Persist:

- symbol;
- fundingRate;
- fundingRateTimestamp.

Never assume an 8-hour funding cycle. Funding intervals differ by instrument.

### Bybit public trade archives

Use official archived public trade data for two purposes:

1. higher-resolution historical price-lattice validation;
2. fallback reconstruction of 1m last-price candles when the Kline API does not retain a Closed/delisted symbol's history.

Reference:
https://bybit-exchange.github.io/docs/v5/market/recent-trade

The Bybit documentation explicitly points to archived historical trades for older public execution data.

#### Trade-archive candle reconstruction

When 1m API klines are unavailable but official trades exist, reconstruct each UTC minute deterministically:

```text
open  = first trade price in timestamp/order sequence
high  = max trade price
low   = min trade price
close = last trade price
volume = sum base quantity
turnover = sum trade quote value
```

Do not synthesize minutes with no trades.

Where both API klines and reconstructed trade candles are available, the audit must cross-check overlapping samples. Record exact OHLCV/turnover differences and a reconciliation status.

A trade-reconstructed series is not silently merged with API candles. Provenance must remain visible per segment.

### U.S. CPI calendar

Official source:
https://www.bls.gov/schedule/news_release/cpi.htm

### U.S. Employment Situation / NFP calendar

Official source:
https://www.bls.gov/schedule/news_release/empsit.htm

### FOMC calendar

Official source:
https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm

No aggregator is canonical for these three event families.

---

## 2. Historical audit period

Required strategy research range:

```text
2022-01-01 00:00:00 UTC
through
the final frozen holdout date
```

Current v0.2 split is defined in [VALIDATION_PROTOCOL.md](VALIDATION_PROTOCOL.md).

The audit must also request enough warm-up history before the first research timestamp to compute all indicators without truncation.

---

## 3. Instrument lifetime model

For each symbol, store:

```text
launch_time
delist_or_delivery_time
status
```

An instrument is eligible on a historical timestamp only if that timestamp lies within its proven lifetime.

### Token migrations / renames

A symbol migration or token rename is treated as a new instrument identity unless Bybit supplies an explicit authoritative continuity mapping that preserves contract identity.

Examples such as an old token symbol being delisted and a replacement symbol being listed must **not** be stitched into one synthetic price history.

Each symbol keeps its own:

- launch time;
- delist time;
- universe membership;
- indicators;
- levels;
- funding history.

A currently Trading symbol must not be treated as if it existed before launchTime.

A Closed symbol must remain present in historical universe reconstruction for periods when it was actually tradable.

This is mandatory to reduce survivorship bias.

---

## 4. Delisted-symbol coverage audit

Before backtesting, enumerate all accessible Trading and Closed USDT linear perpetuals.

For every symbol whose lifetime overlaps the research period, test:

1. whether 1m kline history can be retrieved near launch;
2. whether it can be retrieved at sampled points throughout lifetime;
3. whether it can be retrieved near delisting/delivery;
4. whether funding history is accessible for the relevant lifetime;
5. whether enough data exist to reconstruct the strategy universe without using today's survivors only.

Required report fields:

```text
symbol
status
launch_time
delivery_time
first_1m_timestamp
last_1m_timestamp
expected_1m_count
actual_1m_count
coverage_pct
gap_count
max_gap_minutes
first_funding_timestamp
last_funding_timestamp
funding_event_count
current_tick_size
current_qty_step
historical_price_grid_status
historical_qty_grid_status
bias_flags[]
```

If the API does not retain historical data for some Closed instruments, report it explicitly.

Never silently remove a failed Closed symbol from the historical universe.

---

## 5. 1m continuity rules

The audit must distinguish:

- instrument not yet launched;
- instrument already delisted;
- exchange/data-source outage;
- missing API data;
- a legitimate interval with no reported candle, if such behavior is observed.

Do not forward-fill execution OHLC.

Do not invent a tradable minute.

For strategy simulation, any unresolved 1m gap overlapping:

- a pending entry lifetime;
- an open position;
- a potential stop/target minute;
- a 15m breakout candle used for a decision;

invalidates that simulated setup unless an authoritative source fills the missing data.

Indicator windows affected by unresolved gaps are ineligible until a complete warm-up window exists again.

The same rule applies to objective levels:

- PDH/PDL require a complete previous UTC day;
- PWH/PWL require a complete previous UTC week.

If any unresolved minute gap exists inside the source day/week, the corresponding high/low level pair is unavailable for trading because the missing interval could contain the true extremum.

Do not calculate PDH/PDL/PWH/PWL from incomplete source periods.

The audit should separately report isolated gaps and long outages.

### Live WS ↔ REST candle parity

For future demo/live operation, every 15m decision candle received as WebSocket `confirm=true` must be persisted and reconciled against the corresponding closed REST kline before the v0.2.1 entry activation deadline.

Audit/replay fixtures must test:

- exact normalized OHLCV/turnover match;
- delayed REST availability;
- one-tick/quantity discrepancies;
- hard mismatch handling.

A mismatch is never silently overwritten.

---

## 6. Higher-timeframe aggregation

All strategy bars are derived from 1m data.

### 15m

UTC-aligned quarter hours:

```text
00, 15, 30, 45
```

### 1H

UTC-aligned clock hours.

### Daily

```text
00:00 UTC → next 00:00 UTC
```

### Weekly

```text
Monday 00:00 UTC → next Monday 00:00 UTC
```

Aggregation must be deterministic and tested against known Bybit higher-timeframe bars on sampled periods.

Universe selection uses a fixed cutoff of 09:00 Europe/Amsterdam and only 15m bars with `close_time < cutoff`. The rolling window is 96 bars producing exactly 95 internal close-to-close returns.

Mismatch outside documented rounding/data behavior is a data-pipeline failure.

---

## 7. Historical price-grid contract

v0.2 depends on historical tick size for:

- duplicate-level merging;
- entry normalization;
- stop rounding;
- TP rounding;
- BE rounding;
- deterministic parity between backtest and live.

The current instrument endpoint must **not** be assumed to contain historical tick-size versions.

Resolution order:

1. authoritative historical Bybit instrument metadata, if available;
2. official archived public trades / other official historical market data sufficient to establish the price lattice for that period;
3. an explicitly versioned inference procedure with a confidence flag.

No inferred historical tick size may be labeled authoritative.

### Inference requirements

If inference is necessary, the implementation must:

- operate per symbol and bounded historical period;
- use Decimal arithmetic;
- derive a candidate price lattice from actual traded prices;
- verify that every sampled trade price lies on the candidate lattice;
- detect structural changes over time;
- record the evidence window and confidence.

The smallest observed price difference is not automatically the true tick size and must not be accepted without lattice validation.

If historical tick size remains unresolved for a symbol-period, that period is not eligible for a parity-quality backtest.

---

## 8. Historical quantity-grid contract

Current Bybit metadata exposes qtyStep, minOrderQty, and minNotionalValue, but current values may not equal historical values.

Resolution order:

1. authoritative historical Bybit instrument metadata;
2. another official Bybit historical source;
3. explicit limitation status.

Trade sizes alone are not sufficient proof of the historical qtyStep or minimum-order constraints.

If exact historical quantity rules cannot be reconstructed:

- do not silently substitute today's rules;
- mark the symbol-period `QTY_GRID_UNKNOWN`;
- keep a separate diagnostic simulation only if explicitly labeled non-parity;
- do not use that diagnostic result as the final OOS PASS result until the policy is versioned and frozen.

---

## 9. News-event dataset

Persist every blocked event as:

```text
event_id
event_type
official_release_timestamp
source_timezone
utc_timestamp
source_url
retrieved_at
source_hash_or_snapshot_id
```

Supported event types:

- CPI;
- EMPLOYMENT_SITUATION;
- FOMC_DECISION;
- FOMC_PRESS_CONFERENCE.

BLS schedules use U.S. Eastern time. Conversion must use an IANA timezone such as `America/New_York`, not a fixed UTC offset.

FOMC historical event timestamps must be resolved from official Federal Reserve material.

Backtest and live must consume the same normalized event schema.

---

## 10. Funding dataset

Funding is an event stream, not a fixed schedule generated by the backtester.

Persist exactly the historical:

```text
symbol
fundingRateTimestamp
fundingRate
```

Validation checks:

- timestamps strictly ordered after normalization;
- duplicates removed only by exact identity;
- interval changes detected and reported;
- missing expected events investigated rather than synthesized automatically.

---

## 11. Fees and execution-model inputs

Live maker/taker fees are account-specific and must be read from the live account API for reconciliation and telemetry before live trading.

They must **not** automatically change strategy decisions.

Historical research answers:

> Would this historical strategy have worked under the frozen decision-cost model and execution assumptions we plan to deploy?

### Decision Cost Model

A separately versioned `decision_cost_model` contains the fixed maker fee, taker fee, stop slippage, and market-exit slippage assumptions used by:

- cost-floor qualification;
- sizing;
- cost-adjusted BE;
- canonical decision-ledger risk accounting.

Changing these decision inputs requires an explicit config/spec version change. Live fee APIs may detect drift but may not mutate them automatically.

### Execution scenarios

BASE, STRESS, and SEVERE are shadow execution models. They evaluate the exact same canonical trade path and quantities. Their different fees/slippage do not feed back into trade selection, sizing, reservations, daily-stop state, or future setup availability.

Slippage scenarios must be frozen before validation data are opened.

Execution-model configuration must include at least:

```text
maker_fee_rate
taker_fee_rate
base_stop_slippage
base_market_exit_slippage
stress_stop_slippage
stress_market_exit_slippage
news_stress_window_minutes
news_stress_multiplier_or_model
```

The numerical execution model may be calibrated on development-period evidence only.

---

## 12. News-time stress slippage

CPI and Employment Situation usually occur during the gap between the Europe and US entry sessions, so a Europe position can remain exposed.

A flat all-day slippage assumption is insufficient for stress testing.

The stress execution model must support a separate regime for:

```text
official event time ± 5 minutes
```

For stop-market and other market exits inside this window, use the pre-frozen news-stress slippage model.

The model parameters must be selected and frozen using development data / conservative assumptions before validation is opened.

FOMC events remain present in the calendar even though the current 13:00 New York forced-flat rule normally removes exposure before the standard policy decision time.

---

## 13. Data snapshot immutability

Every research run must identify its exact data snapshot.

At minimum persist:

```text
dataset_version
created_at
source_ranges
symbol_inventory_hash
news_calendar_hash
funding_dataset_hash
mark_price_dataset_hash
price_dataset_manifest_hash
instrument_metadata_hash
code_commit_sha
spec_version
execution_model_version
```

Validation and holdout results are invalid if the underlying dataset silently changes between runs.

Corrections require a new dataset version with a change log.

---

## 14. Audit output

The first project executable should produce a machine-readable audit report plus a human summary.

Suggested artifacts:

```text
artifacts/data-audit/
  summary.json
  symbols.csv
  gaps.csv
  funding.csv
  mark-price-coverage.csv
  kline-trade-reconciliation.csv
  grid-status.csv
  news-events.csv
  manifest.json
```

Summary must answer:

- How many Trading and Closed symbols overlap the research period?
- What share has usable 1m history?
- How many symbol-days are lost to gaps?
- Can delisted symbols be represented?
- Is funding complete enough?
- Is 1m Mark Price history sufficient around funding timestamps?
- How much delisted history was recovered from official trade archives?
- Where API klines and trade-reconstructed klines overlap, do they reconcile?
- For what share of symbol-history is historical tick size authoritative/inferred/unknown?
- For what share is historical qtyStep/minimum metadata authoritative/unknown?
- What known survivorship or execution-parity bias remains?

---

## 15. Data gate

Backtest implementation may begin after the audit tooling exists.

A result may be called **parity-quality OOS** only when all data dependencies used by that result satisfy their frozen policy.

The audit may reveal that some historical constraints cannot be reconstructed exactly. That is acceptable as a finding; silently pretending the limitation does not exist is not.
