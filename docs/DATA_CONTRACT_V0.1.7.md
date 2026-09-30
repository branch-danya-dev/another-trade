# Data Contract v0.1.7

Status: **SUPERSEDED BY [v0.1.8](DATA_CONTRACT_V0.1.8.md) BEFORE VALIDATION OPENED**

This document defines which external facts the project is allowed to trust, how historical coverage is audited, and which missing data block a claim of valid backtest parity.

The canonical trading rules are defined in [SPEC_V0.2.5.md](SPEC_V0.2.5.md). This document must not change strategy behavior.

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

The audit must filter the crypto-perpetual universe explicitly rather than trusting `category=linear` alone.

It must also reconstruct **historical normal-trading start**, because current metadata are insufficient to prove that an instrument was not previously in pre-market trading. Eligible current metadata must satisfy the fail-closed strategy filter:

```text
contractType = LinearPerpetual
quoteCoin = USDT
settleCoin = USDT (where present)
symbolType.casefold() in {"", "innovation"}
isPreListing = false
marketRegion = ""
```

Unknown/new symbolType values are excluded until explicitly reviewed.

Real inventory sampling showed that current `marketRegion=""` is insufficient to exclude TradFi by itself; commodity/forex contracts can have an empty marketRegion. Therefore `symbolType` whitelist and marketRegion must both pass.

The inventory report must count **every returned status**, not only Trading and Closed. Requests made with status=Trading/Closed may still contain other current statuses such as PendingOpen. Unknown statuses are preserved in inventory and excluded from trading eligibility by default.

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
- fundingInterval;
- isPreListing;
- preListingInfo;
- marketRegion;
- first_trade_ms;
- launch_to_first_trade_ms;
- official_continuous_start;
- official_continuous_start_source;
- historical_prelisting_status;
- lifetime_metadata_conflict;
- symbol_identity_notes.

Reference:
https://bybit-exchange.github.io/docs/v5/market/instrument

Important limitation: the current endpoint describes current instrument metadata. It must not be assumed that current tickSize, qtyStep, or minimums were valid throughout the instrument's full historical lifetime.

### launchTime is not a trading-start timestamp

Real sampling showed that many instruments have no 1m candles near `launchTime + 1h`, while others begin only part-way through the sampled window. Therefore:

- `launchTime` is metadata, not the canonical first-trade timestamp;
- audit must derive `first_trade_ms` from observable historical candles;
- warm-up, level availability, and historical eligibility use actual data after `historical_eligibility_start`, not raw launchTime.

Sampling procedure for `first_trade_ms`:

1. start at the UTC day containing launchTime;
2. query aligned daily Kline chunks of at most 1000 days;
3. if a chunk is empty, advance to the next 1000-day chunk;
4. continue until a non-empty daily chunk is found or the proven instrument lifetime ends;
5. locate the earliest returned daily candle in the first non-empty chunk;
6. query at most two aligned 12-hour 1m windows inside that day;
7. earliest observed 1m candle start is `first_trade_ms`.

There is no fixed "first 1000 days only" cutoff. This is required for legacy symbols whose current launchTime metadata is a placeholder such as 2018-01-01 while observable Bybit history begins years later.

Record the full distribution of:

```text
first_trade_ms - launchTime
```

including negative values.

### launchTime quality flag

`launchTime` remains useful metadata, but very large delays can represent placeholder/default values rather than meaningful listing timestamps.

Diagnostic flag:

```text
LAUNCH_TIME_PLACEHOLDER
```

Baseline audit rule:

```text
first_trade_ms - launchTime >= 365 complete days
```

The flag is diagnostic only; it does not change historical eligibility, which already uses `first_trade_ms` / `historical_eligibility_start`.

Symbols flagged `LAUNCH_TIME_PLACEHOLDER` are excluded from launch-delay percentile/tail statistics so placeholder metadata cannot dominate p90/p95/max reporting. The report must separately list them and their raw delays.

### Lifetime/identity conflict probe

For every sampled symbol, query daily candles covering up to the **1000 complete UTC days immediately before the UTC launch day**.

The launch day itself is excluded so a normal first candle whose minute begins before an intraminute launchTime cannot create a false conflict.

If historical candles exist in this pre-launch daily window:

```text
LIFETIME_METADATA_CONFLICT = true
```

For the exact first 1m candle, compare against:

```text
floor_to_minute(launchTime)
```

not the raw millisecond launchTime.

A first 1m candle that starts in the same minute as launchTime is not a lifetime conflict. A candle from an earlier full minute is.

Do not trust current lifetime metadata for parity-quality reconstruction until resolved.

Symbols containing markers such as `OLD` and all unexpected statuses are included as audit-only candidates even if they are not strategy-eligible. This is intended to reveal relist/rename behavior such as an old contract identity being retained under a renamed symbol.

Never stitch an OLD/renamed symbol into a new symbol automatically.

### Known identity relationships / token migrations

Identity relationships discovered during audit are logged explicitly but remain separate instruments.

They are stored in a **versioned configuration file**, not as undocumented Python constants.

Each relationship record must include:

```text
config_version
left_symbol
right_symbol
relation
confidence
automatic_stitching = false
source_urls[]
evidence_notes
config_sha256
```

At minimum the current audit records:

- MATICUSDT → POLUSDT: token migration with overlapping trading lifetime;
- DATAOLD01USDT → DATAUSDT: inferred relist/rename anomaly; DATAOLD01USDT currently exposes no usable Kline history.

A source may establish the economic migration/rebrand without directly proving an internal Bybit alias. In that case `confidence` must remain `inferred` and the note must state the limitation.

Overlapping symbols are not deduplicated automatically in the historical universe. The overlap is a diagnostic that must be reviewed before final universe construction, because two symbols representing the same economic asset could otherwise occupy multiple slots in a same-day ranking.

The inventory/manifest records the relationship, metadata lifetimes, calculated metadata overlap where possible, source URLs, config version/hash, and `automatic_stitching=false`.


### Bybit historical last-price klines

Primary endpoint:

```text
GET /v5/market/kline
category=linear
interval=1
```

Reference:
https://bybit-exchange.github.io/docs/v5/market/kline

The canonical market-price history for both backtest and live **entry decisions** is 1m OHLCV/turnover.

15m, 1H, UTC daily, and UTC weekly bars used by the strategy must be derived from the canonical 1m dataset rather than mixing independent higher-timeframe downloads into strategy calculations.

### Kline request alignment and page semantics

Every Kline request must use interval-aligned `start` and `end` values.

For 1m:

```text
start % 60_000 == 0
end   % 60_000 == 0
```

Equivalent alignment applies to every other fixed interval.

Unaligned request boundaries are rejected before the HTTP call; the client must not depend on undocumented Bybit rounding of milliseconds.

Kline responses are reverse-sorted by startTime. Pagination must:

- deduplicate by exact startTime;
- validate chronological continuity after merge;
- decide whether to continue from the **raw row count / oldest raw startTime**, not the number of candles remaining after unfinished-row filtering or deduplication;
- continue until a raw-empty page or until the oldest raw startTime reaches the requested start boundary.

This prevents a full 1000-row page containing an unfinished/duplicate row from being mistaken for the final page.

### Canonical timestamp convention

Bybit REST kline supplies `startTime`, not strategy close time.

For every fixed-duration bar:

```text
close_time = start_time + interval_duration
bar interval = [start_time, close_time)
```

Never subtract 1 ms.

The WebSocket `end` timestamp is not canonical strategy time.

Consequences that must be covered by tests:

- 08:45–09:00 has `close_time=09:00` and is excluded by a `close_time < 09:00` universe cutoff;
- a 10:00–11:00 1H bar becomes usable exactly at 11:00;
- session inclusion is determined from canonical `close_time`.

### Live decision-source parity

At each 15m entry-decision boundary `T`, live must obtain the complete REST 1m set for `[T-15m, T)` and aggregate the canonical 15m candle using the same code as historical replay.

The rolling 1H/15m indicator state is derived from that same canonical 1m chain.

Native 15m/1H REST and WebSocket candles may be persisted for audit comparisons but are not strategy inputs for entry decisions.

If required 1m REST data are unavailable by `T+60s`, the entry opportunity is rejected with `REJECT_DECISION_DATA_UNAVAILABLE`.

This gives one timestamp convention and one auditable aggregation path.

### Bybit historical mark-price klines

Primary endpoint:

```text
GET /v5/market/mark-price-kline
category=linear
```

The Bybit endpoint supports both `interval=1` and `interval=60`.

Canonical funding-value semantics remain:

- use Mark Price at the exact funding timestamp;
- baseline evidence source remains the OPEN of the 1m Mark Price candle with `startTime == fundingRateTimestamp`;
- fallback remains the CLOSE of the immediately preceding complete 1m Mark Price candle, flagged `MARK_PRICE_1M_APPROX`, until a later version explicitly changes this contract.

### Pilot test: 60m-open equivalence

The bulk pilot must test whether the OPEN of the 60m Mark Price candle beginning at a funding timestamp is exactly equal to the OPEN of the 1m Mark Price candle beginning at the same timestamp.

Pilot procedure:

1. collect actual funding timestamps for every pilot symbol/month;
2. verify/report whether every sampled funding timestamp is aligned to the hour;
3. download the complete pilot-month 60m Mark Price series;
4. for a deterministic sample of funding timestamps across symbols, fetch the corresponding 1m Mark Price candle;
5. compare OPEN values using exact Decimal semantics;
6. report:
   - sample size;
   - timestamp-alignment rate;
   - exact-open equality count/rate;
   - every mismatch with symbol/timestamp/1m-open/60m-open.

The pilot must not silently switch the canonical funding source.

If the predetermined pilot sample demonstrates 100% timestamp alignment and exact OPEN equality with no unresolved mismatch, a subsequent Data Contract version may adopt 60m Mark Price OPEN as the canonical funding-point source for historical bulk research.

Until that version exists, 1m remains canonical.

Reference:
https://bybit-exchange.github.io/docs/v5/market/mark-kline

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

Current v0.2.2 split is defined in [VALIDATION_PROTOCOL_V0.2.2.md](VALIDATION_PROTOCOL_V0.2.2.md).

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

### Historical pre-market state

Current `isPreListing=false` does **not** prove that the beginning of the same symbol's history was ordinary continuous trading. Bybit explicitly documents that `isPreListing` becomes false after a pre-market contract converts to an official contract.

For every instrument whose lifetime could overlap Bybit's pre-market-perpetual feature, the audit must attempt to determine:

```text
official_continuous_start
```

Resolution order:

1. authoritative historical `preListingInfo.phases` when preserved;
2. Bybit official announcement API / official listing or pre-market announcement identifying continuous-trading start;
3. other official Bybit metadata with an explicit timestamp;
4. unresolved status.

Historical eligibility starts at:

```text
max(launchTime, official_continuous_start)
```

when both are known.

If a pre-market history is known/suspected but `official_continuous_start` cannot be proven, mark the affected early segment `PRELISTING_HISTORY_UNKNOWN` and exclude it from parity-quality universe reconstruction.

Do not assume that today's `isPreListing=false` applies retroactively.

A Closed symbol must remain present in historical universe reconstruction for periods when it was actually eligible.

This is mandatory to reduce survivorship and pre-listing bias.

---

## 4. Delisted-symbol coverage audit

Before backtesting, enumerate all accessible Trading and Closed USDT linear perpetuals.

For every symbol whose lifetime overlaps the research period, test:

1. whether 1m kline history can be retrieved near launch;
2. whether it can be retrieved at sampled points throughout lifetime;
3. whether it can be retrieved near delisting/delivery;
4. whether funding history is accessible for the relevant lifetime;
5. whether enough data exist to reconstruct the strategy universe without using today's survivors only;
6. whether historical pre-market/continuous-trading state can be established without applying today's `isPreListing` retroactively.

Required report fields:

```text
symbol
status
launch_time
official_continuous_start
official_continuous_start_source
historical_prelisting_status
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

## 4A. Sample-audit persistence and manifest

Sample coverage is a resumable audit, not a one-shot in-memory job.

Before the first symbol probe, persist a run manifest containing at minimum:

```text
run_id
kind = sample-coverage
sample_only = true
bulk_download = false
created_at_ms
now_ms
inventory_sha256
git_commit_sha
spec_version
data_contract_version
requests_per_second
software_versions.python
software_versions.python_implementation
software_versions.platform
software_versions.tzdata
software_versions.httpx
software_versions.pydantic
software_versions.typer
identity_relationship_config.version
identity_relationship_config.sha256
identity_relationships[]
```

The inventory snapshot used by the run is copied into the run directory.

After **every symbol**, append one JSONL record and `fsync` it. A parser/network failure on the next symbol must not destroy prior results.

JSONL is written as explicit UTF-8 bytes with LF (`0x0A`) line endings. On Windows the descriptor must include binary mode where available so CRT newline translation cannot produce CRLF.

CSV audit artifacts also use an explicit `lineterminator="\n"`.

The same logical run must therefore have identical text-artifact bytes on Windows and Linux, subject only to explicitly recorded software/version differences.

A resumed run must verify that the current inventory hash equals the manifest inventory hash before continuing.

Coverage summary must retain all statuses returned by Bybit; do not report only Trading/Closed totals.

## 5. 1m continuity rules

The audit must distinguish:

- instrument not yet launched;
- instrument already delisted;
- exchange/data-source outage;
- missing API data;
- a legitimate interval with no reported candle, if such behavior is observed.

Do not forward-fill execution OHLC.

Do not invent a tradable minute.

### Zero-volume candles are valid minutes

Real Bybit sampling showed that the API emits 1m candles with:

```text
volume = 0
high = low = open = close = previous close
```

for minutes without trades.

Therefore:

- a present flat zero-volume candle is a valid canonical minute;
- zero volume is **not** a gap;
- absence of the expected 1m candle is a true data gap unless an authoritative fallback source supplies it.

This behavior is important for illiquid instruments: legitimate no-trade minutes must not invalidate indicators/setups merely because volume is zero.

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

Each sample window must include:

```text
expected_count
candle_count
coverage_pct
missing_expected_count
missing_at_start
missing_at_end
internal_gap_count
```

`DATA_PRESENT` means complete expected coverage. Any non-empty but incomplete window is `PARTIAL_DATA`.

The gap detector alone is insufficient because it cannot detect missing candles before the first returned row or after the last returned row in the requested window.

### Live native-candle telemetry

For future demo/live operation, native WebSocket and native REST higher-timeframe candles are persisted as telemetry only.

Entry decisions are built from canonical REST 1m data.

Audit/replay fixtures must test:

- delayed 1m REST availability;
- missing 1m bars at the `T+60s` deadline;
- locally aggregated 15m vs native REST 15m differences;
- locally aggregated 15m vs WebSocket `confirm=true` differences;
- deterministic rejection with `REJECT_DECISION_DATA_UNAVAILABLE`.

Native-candle mismatches are never silently overwritten, but they do not create a live-only trade-selection filter.

### Shadow decision-data availability gate

Before real-money readiness, collect at least:

```text
30 consecutive calendar days
3000 required symbol × decision-boundary evaluations
```

and report:

```text
decision_data_unavailable_count
decision_data_unavailable_rate
later_reconstructed_valid_setups_missed
```

Required readiness threshold:

```text
decision_data_unavailable_rate <= 0.1%
```

The retrospective valid-setup miss count is informational in v0.2.2 and must be reviewed before live approval.

---

### Indicator-chain reset after gaps

An unresolved 1m gap terminates the current canonical indicator segment.

After the gap, derived bars resume only from complete contiguous 1m data.

EMA50 and ATR14 must be re-seeded exactly as specified in SPEC_V0.2.2; no previous pre-gap indicator state crosses the gap.

The audit must expose contiguous-segment identifiers so replay/live code cannot accidentally continue an indicator chain across a known gap.

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

Universe selection uses a fixed cutoff of 09:00 Europe/Amsterdam and only 15m bars with `close_time < cutoff`.

With canonical `close_time = start_time + interval`, the 08:45–09:00 bar is excluded and the last eligible bar is 08:30–08:45.

The rolling window is 96 bars producing exactly 95 internal close-to-close returns.

Mismatch outside documented rounding/data behavior is a data-pipeline failure.

---

## 6A. Numeric reproducibility contract

Canonical decimal arithmetic uses the strategy-defined context:

```text
precision = 50 decimal significant digits
internal rounding = ROUND_HALF_EVEN
```

All exchange-grid rounding remains explicit and directional.

Data parsers must preserve exchange numeric strings as Decimal-compatible text. They must never convert price, quantity, fee, rate, turnover, or funding values through binary float before canonical calculations.

Serialized canonical numeric values use normalized decimal strings.

## 7. Historical price-grid contract

v0.2.2 depends on historical tick size for:

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

## 9A. Funding applicability during audit

Funding probes apply only to perpetual contracts.

For delivery futures / dated contracts:

```text
funding_probe = NOT_APPLICABLE
```

Do not classify the absence of funding on a dated future as missing lifetime data.

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

## 12A. Development-only slippage calibration from public trades

Slippage parameters used by DCM / BASE / STRESS / SEVERE must be calibrated before validation is opened.

Primary calibration source: official Bybit archived public trades from the **development period only**.

### Calibration event universe must be DCM-independent

The event set used to calibrate slippage must not depend on the cost model being calibrated.

A `SLIPPAGE_CALIBRATION_EVENT` is created from every development-period structural setup that passes all market/data rules required to define the entry and stop, including:

- historical universe eligibility;
- allowed objective level and direction;
- closed-candle trend context;
- breakout and volume confirmation;
- session and news filters;
- objective-level room filter;
- required data/grid availability;
- conservative retest LIMIT penetration.

The calibration event deliberately does **not** apply:

- the k=5 cost floor;
- DCM fee/slippage values;
- DCM-derived position sizing;
- portfolio risk/notional capacity;
- daily realized-loss stop;
- daily trade-count capacity;
- any previous-trade PnL-dependent gate.

Once the structural entry event exists, compute the strategy's geometric stop from the same rounded entry/ATR rules as the specification. A stop-trigger calibration observation is recorded whenever subsequent development-period last-price data reach the stop trigger, whether or not that setup would later be admitted by the DCM.

This breaks the circular dependency:

```text
slippage model
→ cost floor / sizing
→ admitted trades
→ observed stops
→ slippage model
```

The calibration population is frozen from market structure before DCM is known.

### Trade-tape execution proxy

Public trade tape is **not** historical order-book depth and must never be described as such.

For each calibration stop event:

1. reference price = rounded stop trigger price;
2. locate the first official public trade at or after the trigger timestamp that is consistent with the market-exit side;
3. proxy execution price = that first qualifying trade price;
4. compute adverse slippage from the stop price in **basis points**.

```text
SELL stop:
max(0, stop_price - proxy_trade_price) / stop_price * 10_000

BUY stop:
max(0, proxy_trade_price - stop_price) / stop_price * 10_000
```

This proxy is intentionally quantity-independent and is appropriate only as a small-order / tape-latency estimate. It does not model queue position, visible or hidden depth, market impact, or the fill of a large order across multiple book levels.

Do not consume subsequent tape volume to pretend that historical order-book depth is known.

For diagnostics, additionally record:

- milliseconds from stop trigger to first qualifying trade;
- min/max qualifying trade price over the next 2 seconds;
- count and base/quote turnover of qualifying trades over the next 2 seconds.

These diagnostics may characterize local tape stress but do not convert the tape into an order-book reconstruction.

### Calibration regimes

At minimum separate:

- standard regime;
- official macro-news window ±5 minutes.

Report slippage-bps distributions by:

- symbol;
- session;
- turnover/liquidity bucket;
- exit side;
- news/non-news regime.

Planned notional is **not** used to select the calibration event population or the primary slippage quantile, because planned quantity depends on the DCM.

### Frozen quantiles

After development calibration and before opening validation:

- DCM decision slippage = development p75 of the applicable standard-regime stop-event bps distribution;
- BASE execution slippage = p75;
- STRESS execution slippage = p95;
- SEVERE execution slippage = p99.

If regime/bucket coverage is insufficient, use the next broader conservative bucket and record the fallback.

No validation/holdout event may be used to recalibrate these quantiles.

The calibration artifact must store:

- exact structural-event definition/version;
- number of pre-cost calibration setups;
- number of stop-trigger observations;
- exact archive files;
- code commit;
- bucket definitions;
- quantiles in basis points;
- explicit limitation `NO_HISTORICAL_ORDERBOOK_DEPTH`.

The artifact and its inputs are hashed into the execution-model manifest.

## 12B. Cache immutability horizon

Historical API responses are cached as immutable only when the entire requested page is at least **24 hours older than audit now_ms**.

Closed data newer than 24 hours remain uncached because exchanges can revise recent history.

Empty Kline/funding responses are never cached as immutable facts.

Raw response bytes, request parameters, and SHA-256 remain part of every cached entry.

## 12C. Parquet reproducibility contract

The pilot/bulk implementation pins:

```text
pyarrow == 25.0.1
```

and records the exact writer configuration in every partition manifest.

At minimum freeze:

- Parquet format/version;
- compression codec and level;
- row-group size;
- dictionary encoding policy;
- statistics policy;
- data-page version;
- canonical schema including decimal precision/scale;
- sorted row order.

The **primary partition identity** is not the raw Parquet-file SHA.

Primary identity:

```text
logical_content_sha256
```

computed from a canonical serialization of rows sorted by `start_ms` using fixed field order and exact normalized decimal text / fixed-scale values.

The Parquet file SHA-256 is also recorded, but it is secondary:

```text
parquet_file_sha256
```

A writer/library upgrade may legitimately change file bytes without changing logical content. Such an upgrade still requires a versioned dataset/tooling change and must preserve the logical-content hash.

## 12D. Partition lifecycle

A monthly partition has one of:

```text
OPEN
SEALED
```

A partition ending at `partition_end` is eligible for `SEALED` only when:

```text
audit_now >= partition_end + 24 hours
```

Before that time it remains `OPEN`, even if the calendar month itself has ended.

OPEN partitions:

- may be downloaded/resumed;
- must not be treated as immutable;
- must be revalidated before sealing;
- do not contribute an immutable final-dataset hash.

SEALED partitions:

- have complete manifest/hash state;
- are immutable unless an explicit dataset correction/version is created.

This prevents the current or just-ended month from being mistaken for a finalized immutable partition.

## 12C. Pilot gate before full bulk download

Before starting the complete multi-year universe download, run a bounded pilot.

Minimum pilot set:

```text
BTCUSDT
+ 5–10 additional symbols
including both currently Trading and Closed instruments
```

Minimum time range:

```text
one complete UTC calendar month per pilot symbol
```

The pilot must verify:

1. compressed raw-response container layout;
2. per-response request metadata and SHA-256 index;
3. canonical Parquet `symbol/year-month` partitioning;
4. exact integer/fixed-scale round-trip for prices/volume/turnover;
5. forced interruption and resume without duplicate/missing pages;
6. partition/page manifest recovery after interruption;
7. full minute continuity accounting including valid zero-volume candles;
8. deterministic 1m → 15m/1H/D aggregation;
9. sampled comparison against native Bybit 15m/1H/D bars;
10. logical-content hash reproducibility of completed immutable pilot partitions on rerun;
11. secondary Parquet-file hash reproducibility under the pinned writer/toolchain;
12. 1m-vs-60m Mark Price OPEN equivalence at deterministic sampled funding timestamps;
13. OPEN/SEALED partition lifecycle behavior around the 24h immutability horizon.

Do not start the estimated full ~hundreds-of-millions-row download until the pilot artifacts and resume test pass.

## 12E. Bulk-download storage policy

The sample-audit request cache is not the permanent storage format for the full 1m dataset.

A complete history can require on the order of hundreds of millions of 1m rows and hundreds of thousands of API pages. Persisting every response as two standalone files would create excessive filesystem metadata overhead, especially on NTFS.

For the bulk-download phase:

### Raw source retention

- preserve original response bytes for reproducibility;
- compress raw responses;
- group them into containers partitioned at least by `symbol / calendar-month`;
- maintain an index entry for every original API response containing:
  - exact request parameters;
  - response byte offset/length inside the container;
  - uncompressed SHA-256;
  - compressed-container SHA-256;
  - capture timestamp / source endpoint;
- never use compression/containerization to change the logical raw bytes used for hashing.

The exact container format must be selected and benchmarked during bulk-downloader implementation. One-file-per-API-page is explicitly not the target permanent layout.

### Canonical analytical dataset

Canonical normalized 1m data are written directly into Parquet partitioned by:

```text
symbol / year-month
```

or another equivalently bounded symbol/month partition scheme proven by benchmark.

Prices/quantities use the exact integer/fixed-scale representation defined by the numeric data layer; strategy-level Decimal reconstruction remains exact.

Each Parquet partition manifest records:

- symbol;
- UTC month;
- first/last minute;
- row count;
- unresolved gap count;
- source raw-container references;
- source-response hashes;
- partition file hash;
- data-contract version.

Bulk download must be resumable at partition/page granularity and must never require restarting completed immutable months.

## 12F. Entry-activation parity inputs

Historical strategy replay must retain the OPEN of the 1m candle whose `open_time == entry_activation_time`.

SPEC v0.2.5 uses that value for the canonical PostOnly marketability gate:

```text
LONG:  activation_open <= entry_price → REJECT_ACTIVATION_THROUGH_LEVEL
SHORT: activation_open >= entry_price → REJECT_ACTIVATION_THROUGH_LEVEL
```

No extra tick-history source is invented for this rule.

Forward/demo/live telemetry must compare the canonical prediction with the actual Bybit PostOnly outcome and report mismatch counts/rates separately from strategy PnL.

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
  prelisting-history.csv
  decision-data-availability.csv
  slippage-calibration.json
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
- Which symbol-periods have proven normal continuous-trading start vs PRELISTING_HISTORY_UNKNOWN?
- Where API klines and trade-reconstructed klines overlap, do they reconcile?
- For what share of symbol-history is historical tick size authoritative/inferred/unknown?
- For what share is historical qtyStep/minimum metadata authoritative/unknown?
- What known survivorship or execution-parity bias remains?

---

## 15. Data gate

Backtest implementation may begin after the audit tooling exists.

A result may be called **parity-quality OOS** only when all data dependencies used by that result satisfy their frozen policy.

The audit may reveal that some historical constraints cannot be reconstructed exactly. That is acceptable as a finding; silently pretending the limitation does not exist is not.


## 16. First real sampling findings that changed this contract

The initial sample-coverage run produced enough evidence to invalidate several earlier assumptions before validation was opened:

- launchTime frequently precedes observable first-trade data;
- marketRegion alone does not exclude all TradFi perpetuals;
- requested Trading/Closed inventory pages may contain other statuses;
- relist/rename history can make current lifetime metadata unreliable;
- four legacy high-liquidity symbols demonstrated that first-trade discovery must scan beyond the first 1000 days;
- intraminute launchTime values require minute-normalized conflict detection;
- dated futures require funding=NOT_APPLICABLE rather than missing-data classification;
- token migrations can have overlapping lifetimes and must be logged without automatic stitching;
- Bybit may tolerate/round unaligned millisecond Kline boundaries;
- sample audit must be checkpointed because one malformed/network response can otherwise lose hours of completed work.

These are development-phase data-contract corrections, not validation-driven strategy tuning.


## 17. Second sampling findings

The second completed sample-coverage run confirmed:

- first_trade_ms resolved for all strategy-eligible symbols;
- all sampled eligible near-first/mid-life/near-end windows were complete;
- no eligible lifetime metadata conflicts remained after minute-normalized comparison;
- valid zero-volume flat candles are emitted explicitly by Bybit, so missing candles are true gaps rather than ordinary no-trade minutes;
- legacy launchTime placeholders require diagnostic separation from meaningful launch-delay statistics;
- token-identity relationships must carry versioned source provenance;
- sample audit is sufficiently clean to proceed to a bounded bulk-download pilot, but not directly to an untested full-universe storage run.


## 18. Activation-batch data requirements

SPEC v0.2.5 moves state-dependent admission to `entry_activation_time`.

Historical replay must therefore provide, at every activation boundary:

- activation 1m OPEN for PostOnly marketability;
- complete execution outcomes for already-active pending orders in the just-completed minute;
- complete execution outcomes for already-open positions in that minute;
- boundary funding events;
- close-derived/session-end exits due at the boundary;
- canonical realized equity/daily-PnL state after those events;
- news blackout state at activation time.

The dataset/replay layer must preserve enough event ordering metadata to implement the portfolio-wide minute ordering in SPEC v0.2.5 deterministically.

No candidate may be sized using signal-time equity when the specification requires activation-time equity.
