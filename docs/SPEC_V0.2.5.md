# Trading Specification v0.2.5

Status: **SUPERSEDED BY [v0.2.6](SPEC_V0.2.6.md) BEFORE VALIDATION OPENED**

Supersedes v0.2.4 before validation was opened. Earlier versions remain archived for traceability.

This document is the canonical trading specification. Code must implement it literally. Any behavioral change requires a new specification version before implementation.

## 1. Scope

One strategy only:

**intraday trend-following breakout and retest of objective previous-period levels.**

No discretionary level quality, order-flow score, manual override, ML prediction, density confirmation, round-number heuristic, local pivot clustering, or inherited logic from previous trading-bot projects is part of v0.2.5.

Market: Bybit USDT linear perpetuals.

BTC is context only and is not traded by v0.2.5.

---

## 2. Time model

All stored timestamps are UTC.

### Canonical bar interval and close_time

Every bar is represented as a half-open interval:

```text
[start_time, close_time)
```

Canonical close time is defined as:

```text
close_time = start_time + exact_interval_duration
```

There is no `-1 ms` convention anywhere in strategy logic.

Examples:

```text
15m bar start 08:45:00 → close_time 09:00:00
1H  bar start 10:00:00 → close_time 11:00:00
```

Bybit REST kline `startTime` is converted using this rule.

Bybit WebSocket may expose an inclusive `end` value such as `close_time - 1 ms`; that field is transport metadata only and must never become the canonical strategy `close_time`.

A bar is complete and may be used by a decision at timestamp `T` iff:

```text
bar.close_time <= T
```

A bar belongs to a session/cutoff test according to its canonical `close_time`.

### Strategy day

A strategy day is the UTC calendar date.

Daily counters, level-consumption state, and daily realized-equity snapshot reset at 00:00 UTC.

### Daily levels

- PDH: high of the previous complete UTC day.
- PDL: low of the previous complete UTC day.

A UTC day is [00:00, 24:00).

### Weekly levels

- PWH: high of the previous complete UTC week.
- PWL: low of the previous complete UTC week.

A UTC week starts Monday 00:00 UTC and ends the next Monday 00:00 UTC.

### Entry sessions

Europe session:

```text
09:00 inclusive → 12:00 exclusive
timezone: Europe/Amsterdam
```

US session:

```text
08:30 inclusive → 13:00 exclusive
timezone: America/New_York
```

Timezone database rules must be used. DST must never be implemented by hard-coded seasonal offsets.

A breakout is eligible only when its 15m candle closes inside an entry session.

A pending entry created in a session expires at the earlier of:

```text
breakout_close + 120 minutes
originating_session_end
```

No new entries outside those windows.

All open positions are force-closed at 13:00 America/New_York.

A Europe-session position may remain open between the Europe and US entry windows.

---

## 3. Universe selection

Universe is frozen once per strategy day at a deterministic cutoff:

```text
universe_cutoff = 09:00 Europe/Amsterdam
```

Only 15m bars with `close_time < universe_cutoff` may enter universe ranking.

Because `close_time = start_time + interval`, the 08:45–09:00 bar has `close_time = 09:00` and is excluded.

Therefore the last eligible 15m bar is 08:30–08:45. The first Europe-session breakout candle cannot influence its own universe membership.

Eligible instruments must satisfy all of the following as-of the historical selection timestamp:

- Bybit category = `linear`;
- `contractType = LinearPerpetual`;
- `quoteCoin = USDT`;
- `settleCoin = USDT` where the field is available;
- `symbolType.casefold() ∈ {"", "innovation"}`;
- `isPreListing = false` in the current snapshot;
- `marketRegion = ""`;
- historical status is proven to be normal continuous trading at that timestamp;
- no expiry/futures contract is admitted;
- sufficient prior data exist to compute all required levels, EMA, ATR, volume ratio, turnover, and volatility;
- BTCUSDT is excluded from the tradable universe.

Unknown/new `symbolType` values are excluded by default until explicitly reviewed. Current metadata alone are not sufficient to establish historical eligibility.

### Historical trading start

`launchTime` is metadata only and is **not** treated as proof that trades existed from that instant.

For each instrument the data layer must establish:

```text
first_trade_ms
official_continuous_start
historical_eligibility_start
```

where:

```text
historical_eligibility_start =
max(first_trade_ms, official_continuous_start)
```

when an official pre-market → continuous transition is known.

If no pre-market period is known, `first_trade_ms` is the earliest tradable-data anchor.

Indicator warm-up, PDH/PDL/PWH/PWL availability, and historical universe membership may begin only after the instrument has sufficient complete data following `historical_eligibility_start`.

If current metadata conflict with observable historical candles (for example data exist before current `launchTime`, or an `*OLD*` symbol has history inconsistent with its current metadata), the affected history is quarantined until its identity/lifetime is resolved.

Pre-market contracts, TradFi perpetuals, delivery futures, event contracts, unknown instrument families, and other non-crypto derivatives are not eligible.

### Stage 1: liquidity

Using the previous 96 fully closed 15m bars:

```text
turnover_24h = sum(turnover[-96:])
```

Take the top 20 instruments by turnover.

### Stage 2: volatility

For the same 96 closed 15m bars there are exactly 95 close-to-close log returns:

```text
r_t = ln(close_t / close_(t-1))
RV24 = sample_std(the 95 returns formed inside the 96-bar window)
```

Use the sample standard deviation with denominator `n - 1`.

Take the top 5 by RV24 from the top-20 turnover set.

Tie-break: higher turnover wins. Final deterministic tie-break: symbol ascending.

Historical spread is not a strategy filter in v0.2.5.

Live spread may be recorded as telemetry but may not alter decisions.

---

## 4. Objective levels and allowed directions

Only four source levels exist:

- PDH
- PDL
- PWH
- PWL

Allowed breakout directions are intentionally restricted:

```text
LONG  → PDH or PWH only
SHORT → PDL or PWL only
```

A reclaim of PDL from below is **not** a LONG breakout.

A rejection/reclaim of PDH from above is **not** a SHORT breakout.

Those are different patterns and are outside v0.2.5.

### Duplicate levels

Levels on the same allowed side are considered duplicates if:

```text
abs(level_a - level_b) <= historical_tick_size
```

This is not a tunable epsilon. It means there is no more than one exchange price increment between them.

Duplicate levels are represented as one canonical level while preserving all source labels.

Canonical price:

```text
LONG-side cluster  → highest price in the cluster
SHORT-side cluster → lowest price in the cluster
```

This is the farther price in the intended breakout direction.

Historical tick size must come from the validated Data Contract. No silent substitution with today's tick size is allowed.

### Multiple levels crossed by one breakout candle

A LONG breakout candle crosses a level when:

```text
previous_15m_close <= level
current_15m_close  >  level
```

A SHORT breakout candle crosses a level when:

```text
previous_15m_close >= level
current_15m_close  <  level
```

If one 15m close crosses multiple distinct eligible levels:

- LONG uses the highest crossed level as the setup level;
- SHORT uses the lowest crossed level as the setup level.

If the selected setup becomes valid and its entry LIMIT is successfully created, **all eligible same-direction objective levels crossed by that candle are consumed for the strategy day**.

If the selected setup fails before order creation, no crossed level is consumed.

### Level consumption

A level is consumed only when a fully valid setup reaches successful entry-order creation.

These do not consume a level:

- raw price crossing;
- activation-time PostOnly rejection / `REJECT_ACTIVATION_THROUGH_LEVEL`;
- wrong trend;
- insufficient breakout volume;
- outside-session breakout;
- news blackout;
- cost-floor failure;
- insufficient room to the next level;
- capacity/risk rejection before order creation.

After a level is consumed, no second setup may use it during the same strategy day.

---

## 5. Trend context

All trend decisions use only fully closed 1H candles.

At a 15m decision timestamp:

```text
latest_1h = latest 1H candle with close_time <= decision_time
```

The currently forming 1H candle is forbidden.

This rule applies independently to the traded alt and BTC.

### LONG context

For the alt:

```text
close_1h > EMA50_1h
EMA50[t] > EMA50[t-3]
```

For BTC:

```text
close_1h > EMA50_1h
EMA50[t] > EMA50[t-3]
```

Both must pass.

### SHORT context

For the alt:

```text
close_1h < EMA50_1h
EMA50[t] < EMA50[t-3]
```

For BTC:

```text
close_1h < EMA50_1h
EMA50[t] < EMA50[t-3]
```

Both must pass.

EMA is computed exclusively from closed 1H bars.

### EMA50 exact definition

For each instrument, the canonical EMA series starts from the earliest complete 1H history admitted by the Data Contract.

```text
alpha = 2 / (50 + 1)
EMA50[first_defined_bar] = SMA(first 50 closed 1H closes)
EMA50[t] = alpha * close[t] + (1 - alpha) * EMA50[t-1]
```

The first EMA value is therefore defined only after 50 complete 1H closes.

Live/demo must not re-seed EMA from an arbitrary recent window after restart. It must either:

1. restore the persisted canonical EMA state derived from the same historical chain; or
2. deterministically recompute from the canonical historical anchor.

### Canonical live decision bars

Backtest and live entry decisions use the same bar-construction path.

Canonical raw decision data are **REST 1m last-price klines**.

At every 15m decision boundary `T`:

1. WebSocket `confirm=true` may wake the decision scheduler and is persisted for telemetry;
2. the live engine fetches/refreshes the complete REST 1m set required for the just-closed interval `[T-15m, T)`;
3. the canonical 15m bar is aggregated locally from exactly 15 complete REST 1m bars using the same aggregation function as historical backtest;
4. 1H context is likewise derived from the canonical 1m chain, never from a separate native 1H decision source;
5. if all required canonical 1m bars are not available by `T + 60 seconds`, reject the entry opportunity with `REJECT_DECISION_DATA_UNAVAILABLE`.

Native 15m WebSocket and native 15m REST candles are comparison/telemetry sources only. Differences between native candles and the locally aggregated canonical bar are logged but **do not reject an otherwise valid setup**.

No byte-for-byte WS↔REST equality gate is part of strategy selection.

The one-minute deadline defines deterministic entry activation; see Section 15.

### Live decision-data readiness gate

Before real-money trading, shadow operation must demonstrate over at least:

```text
30 consecutive calendar days
AND
3000 required symbol × 15m decision-boundary evaluations
```

that:

```text
REJECT_DECISION_DATA_UNAVAILABLE / required_decision_evaluations <= 0.001
```

i.e. no more than 0.1%.

Every data-unavailable boundary must be reconstructed later when data become available to determine whether it hid a valid setup. That retrospective count is reported separately.

Exceeding the 0.1% rate blocks real-money readiness but does not change the historical strategy.

---

## 6. Breakout

Breakout is evaluated only after a 15m candle is confirmed closed.

### LONG

```text
previous_close <= selected_level
current_close  >  selected_level
```

### SHORT

```text
previous_close >= selected_level
current_close  <  selected_level
```

### Volume confirmation

```text
volume_ratio =
current_15m_volume /
mean(previous 20 completed 15m volumes)
```

The breakout candle is excluded from the 20-bar average.

Requirement:

```text
volume_ratio >= 1.5
```

The volume ratio must be logged with local time-of-day for later diagnostics. v0.2.5 does not normalize volume by time-of-day.

---

## 7. News blackout

Blocked U.S. macro events:

- CPI;
- Employment Situation / NFP;
- FOMC policy decision;
- FOMC press conference.

Blackout is the half-open interval:

```text
[event_time - 30 minutes, event_time + 30 minutes)
```

A decision or entry activation exactly at `event_time + 30 minutes` is outside the blackout.

During blackout:

- no new setup may be created;
- no new entry may occur intentionally;
- all unfilled entry LIMIT orders are cancelled at blackout start.

A cancelled pending order does not restore a consumed level.

Open positions are not force-closed for news. Existing exchange stops, targets, false-break exits, and session-end exit remain active.

Under the current session schedule, ordinary 14:00 New York FOMC decisions occur after the 13:00 forced-flat time and therefore normally have no direct exposure effect. They remain in the calendar contract for correctness and future strategy versions.

Official calendar sources are defined in the Data Contract.

---

## 8. Initial stop and price risk

Raw stop:

```text
LONG:
stop_raw = selected_level - 0.5 * ATR14_15m

SHORT:
stop_raw = selected_level + 0.5 * ATR14_15m
```

ATR uses only information available when the breakout candle closes.

### ATR14 exact definition

ATR14 uses Wilder's True Range and Wilder smoothing (RMA), not SMA or EMA smoothing.

```text
TR[t] = max(
    high[t] - low[t],
    abs(high[t] - close[t-1]),
    abs(low[t] - close[t-1])
)

ATR14[first_defined_bar] = SMA(first 14 valid TR values)
ATR14[t] = (ATR14[t-1] * 13 + TR[t]) / 14
```

ATR is calculated from closed 15m bars only. The canonical ATR chain is persisted/recomputed from the same deterministic historical anchor; live restart must not seed it from an arbitrary short recent window.

### Indicator reset after an unresolved data gap

An unresolved 1m gap breaks the indicator chain.

Derived 15m/1H bars touching the gap are invalid and may not be used.

After the gap:

**EMA50**

- start a new contiguous 1H segment;
- collect 50 consecutive complete 1H closes;
- seed EMA with their SMA;
- continue recursively using alpha `2/51`;
- trend is not eligible until both `EMA[t]` and `EMA[t-3]` exist, so at least 53 consecutive complete post-gap 1H bars are required for the slope condition.

**ATR14**

- start a new contiguous 15m segment;
- the first complete post-gap bar provides the previous-close anchor;
- collect 14 subsequent valid TR values;
- seed Wilder ATR with the SMA of those 14 TR values;
- therefore the first post-gap ATR requires 15 consecutive complete 15m bars.

Backtest, shadow, demo, and live use identical reset behavior.

After price rounding:

```text
R_price = abs(entry_price - stop_price)
```

R_price is the strategy's geometric price-risk unit.

It is distinct from all-in monetary risk and from reported net R.

---

## 9. Price-grid rules

All calculations use exact decimal arithmetic, never binary floating-point rounding.

Canonical numeric context:

```text
precision = 50 significant decimal digits
internal rounding = ROUND_HALF_EVEN
```

Exchange-grid operations never rely on the context's default rounding. They explicitly use the directional floor/ceiling rules defined below.

All constants such as `0.5`, `1.5`, fee rates, and slippage rates are constructed from decimal strings, not binary floats.

Historical tick size must be resolved before a historical order can be simulated.

### Entry

The objective level should already be an exchange-traded price. For defensive normalization:

```text
LONG buy LIMIT  → round DOWN to tick
SHORT sell LIMIT → round UP to tick
```

This rounds away from a more aggressive fill.

### Initial stop

Round farther away from entry:

```text
LONG stop  → round DOWN
SHORT stop → round UP
```

### TP1

Compute TP1 only after entry and stop are rounded and R_price is final:

```text
LONG raw TP1  = entry + 2 * R_price
SHORT raw TP1 = entry - 2 * R_price
```

Round farther in the profit direction:

```text
LONG TP1  → round UP
SHORT TP1 → round DOWN
```

### Objective TP2

A next objective level is normalized toward the current position:

```text
LONG TP2 objective  → round DOWN
SHORT TP2 objective → round UP
```

The room filter is evaluated with the rounded entry, rounded stop, and rounded objective target.

### Cost-adjusted break-even stop

The raw BE price is the price at which the remaining quantity has expected net PnL of approximately zero after its pro-rata entry fee plus expected taker exit fee and configured base stop slippage.

Round far enough into profit to preserve non-negative expected BE:

```text
LONG BE  → round UP
SHORT BE → round DOWN
```

### Quantity

Quantity is calculated only after every price used by sizing has been rounded.

Final quantity is always rounded DOWN to the historical quantity step.

If the resulting order violates historical minimum quantity/notional rules, the setup is rejected.

No current exchange grid may silently replace unknown historical metadata.

---

## 10. Frozen decision-cost model and minimum-R filter

Decision-affecting costs are separated from realized execution scenarios.

### Decision Cost Model (DCM)

The DCM is a versioned, hashed strategy configuration containing fixed constants for:

- decision maker fee rate;
- decision taker fee rate;
- decision stop slippage;
- decision market-exit slippage.

The DCM is used identically by historical backtest, demo, and live for:

- `C_stop`;
- the k=5 cost floor;
- position sizing;
- cost-adjusted BE;
- decision-ledger risk accounting.

Live account fee APIs are telemetry/reconciliation inputs and **must not automatically replace DCM values**.

If the real fee schedule changes, the strategy does not silently mutate. A deliberate DCM configuration version change is required before the new values may affect decisions. If observed live costs exceed the frozen DCM assumptions, new entries are blocked until the drift is resolved or a new approved config version is deployed.

Define expected stop cost per unit:

```text
C_stop =
entry maker fee
+ expected stop taker fee
+ expected stop slippage
```

All terms are expressed in quote-currency loss per one unit of quantity.

Baseline cost-floor constant:

```text
k_cost = 5
```

Required:

```text
R_price >= k_cost * C_stop
```

Robustness neighbors are 4 and 6. They are not used to choose a best-performing baseline after holdout inspection.

Expected all-in loss per unit:

```text
L_unit = R_price + C_stop
```

### Execution scenarios do not change the strategy path

BASE, STRESS, and SEVERE are shadow execution ledgers applied to the same canonical decision path.

They may change simulated realized PnL through fees/slippage assumptions, including news-time stress, but they may **not** feed back into:

- signal qualification;
- cost-floor decisions;
- trade-set membership;
- quantity sizing;
- slot/risk reservations;
- the canonical daily-loss gate;
- subsequent setup availability.

The canonical decision path uses the frozen DCM. This prevents STRESS from becoming a different strategy with a different trade sample.

Live actual fills/fees are recorded in a separate actual-execution ledger. Safety controls may only reduce or block risk relative to the canonical decision path; every such intervention is logged as a parity deviation.

### Live cost-drift gate

Fee drift is evaluated from current account fee rates:

```text
fee_drift = actual_rate > DCM_rate + 0.000001
```

where rates are decimal fractions and `0.000001 = 0.01 bp`.

If maker or taker fee drift is true, block new entries until reviewed or a new approved DCM version is deployed.

Slippage drift is measured separately for standard-regime market exits.

For each exit:

```text
adverse_slippage_bps =
max(0, adverse_price_difference / reference_price * 10000)
```

Reference price:

- stop/BE exit: configured stop trigger price;
- false-break/session market exit: last traded price captured at order submission.

News-window exits inside official event time ±5 minutes are logged in a separate regime and do not enter the standard rolling gate.

For each exit class, once at least 20 standard-regime observations exist, compute the median of the most recent 20.

Block new entries if:

```text
median_20_bps >
max(
    1.25 * DCM_expected_slippage_bps,
    DCM_expected_slippage_bps + 2.0
)
```

A single outlier before 20 observations raises an alert but does not by itself redefine the model or automatically trigger the rolling-drift block.

A drift block does not auto-clear from later favorable fills; it requires explicit review.

Execution-model constants and scenario parameters are versioned and frozen before validation data are opened.

---

## 11. Room to next objective level

Find the nearest eligible objective level in the trade direction.

For target selection, PDH, PDL, PWH, and PWL are all candidates regardless of whether they are a "high-side" or "low-side" breakout level. A PDL/PWL may therefore be a LONG target if it is objectively above entry, and a PDH/PWH may be a SHORT target if it is objectively below entry.

Exclude only the objective levels crossed by the **current breakout candle** and merged into/consumed by the current breakout event.

A level consumed by an earlier setup on the same strategy day remains eligible as a target if it is not part of the current breakout event.

LONG:

```text
nearest objective price > entry
```

SHORT:

```text
nearest objective price < entry
```

After grid normalization, reject the setup if:

```text
distance(entry, next_level_target) < 2 * R_price
```

If no objective level exists farther in the trade direction, the setup may proceed.

If the objective target is exactly 2R after rounding, the setup is valid. TP1 and TP2 may coincide; in that rare case the entire remaining executable quantity may be closed at that common target according to normal fill rules.

---

## 12. Position sizing

### Historical/backtest equity

In historical research there is one canonical realized-equity ledger:

```text
canonical_realized_equity =
initial_equity
+ canonical realized closed trading PnL
- canonical realized fees
+/- canonical realized funding
```

Unrealized PnL is excluded.

For historical/backtest mode:

```text
risk_equity = canonical_realized_equity
```

### Live dual-equity rule

Live/demo with real exchange execution maintain two ledgers:

```text
canonical_realized_equity
actual_realized_equity
```

`canonical_realized_equity` advances from the frozen DCM/canonical decision path.

`actual_realized_equity` starts from the approved live starting capital and advances only from actual realized fills, actual fees, and actual funding. Unrealized PnL is excluded.

For every new live risk decision:

```text
risk_equity =
min(canonical_realized_equity, actual_realized_equity)
```

All risk budgets, risk-capacity limits, and strategy notional caps use `risk_equity` in live mode.

This means favorable real execution cannot increase risk above the canonical strategy, while worse real execution immediately reduces future risk.

External deposits, withdrawals, manual transfers, or unexplained balance changes do not silently alter `actual_realized_equity`. They set `CAPITAL_REBASE_REQUIRED` and block new entries until an explicit reconciled rebase is approved, normally effective from the next strategy day.

Per-trade risk budget:

```text
risk_budget = risk_equity * 0.005
```

Risk-sized quantity:

```text
qty_risk = risk_budget / L_unit
```

Then apply portfolio-risk and notional caps:

```text
max_total_reserved_risk = 0.01 * risk_equity
remaining_risk_capacity =
    max(0, max_total_reserved_risk - sum(existing_initial_risk_reservations))

qty_risk_capacity =
    remaining_risk_capacity / L_unit

max_position_notional = 2.0 * risk_equity
max_total_reserved_notional = 3.0 * risk_equity
```

Final pre-round quantity:

```text
qty = min(
    qty_risk,
    qty_risk_capacity,
    max_position_notional / entry,
    remaining_portfolio_notional / entry
)
```

If `remaining_risk_capacity <= 0`, reject with `REJECT_RISK_CAPACITY`.

Otherwise risk-capacity behaves like the notional cap: it reduces quantity rather than rejecting the setup. Reject only if the resulting rounded quantity violates the exchange minimums.

Then round quantity down to historical qtyStep.

If notional caps reduce size, the trade is still allowed as long as exchange minimums are met.

The setup records both intended and actual monetary risk.

---

## 13. Capacity, reservations, and daily trade count

Maximum active strategy slots:

```text
max_slots = 2
```

A slot is occupied by:

- pending entry LIMIT;
- partially filled entry;
- open position;
- position after TP1;
- runner protected by BE.

A slot is released only when:

- an entirely unfilled entry is cancelled/expired; or
- the position is completely closed.

Pending entries reserve:

- one active slot;
- their initial actual risk reservation;
- their actual notional reservation;
- one unit of remaining daily trade capacity.

Maximum total reserved initial risk is continuously recomputed against current realized equity:

```text
max_total_reserved_risk = 1.0% of current risk_equity
```

Existing reservations are not retroactively resized after equity changes. A new setup uses only the remaining capacity defined in Section 12.

A position keeps its original reservation until fully closed, even after TP1 or BE movement.

### Daily trade limit

```text
max_filled_setups_per_day = 3
```

A setup counts as filled on its first non-zero entry fill.

Invariant:

```text
filled_setups_today + pending_entry_setups <= 3
```

Therefore pending entries cannot create a race that produces a fourth daily trade.

On the third first-fill event:

- block all further setup creation for the strategy day;
- cancel every other still-unfilled entry order as a safety action.

A partially filled third setup may continue filling its already-open order according to live order-management rules; it is still one setup, not multiple trades.

---

## 14. Candidate creation and activation-batch admission

A confirmed breakout at signal time `T` creates an **activation candidate**, not a pending order.

At `T`, freeze only market-derived setup facts that depend on data closed at or before `T`, including:

- historical universe membership;
- selected objective level and crossed-level set;
- direction;
- alt/BTC trend result;
- breakout volume ratio;
- ATR-derived raw/rounded entry and stop;
- room result;
- DCM cost-floor result;
- originating session and expiry;
- frozen daily turnover ranking key;
- frozen breakout volume-ratio ranking key.

Candidate creation at `T` does **not**:

- consume an objective level;
- reserve a slot;
- reserve risk/notional;
- reserve daily trade capacity;
- size the position from equity;
- create an exchange order.

Canonical admission occurs once, at:

```text
entry_activation_time = T + 60 seconds
```

### Portfolio event order before an activation batch

At every 1m boundary `U`, before admitting candidates whose activation time is `U`, process the just-completed minute `[U-60s, U)` and boundary events in this deterministic order:

1. **Pending entries already active at the start of the minute:** if their historical fill condition is reachable in that minute, apply the fill before any cancellation caused by events from the same minute.
2. **Already-open positions:** resolve intrabar events using Sections 16–18.
3. **Cross-symbol daily-stop pessimism:** for purposes of latching the realized daily-loss stop, apply realized-loss events from already-open positions before realized-profit events when their cross-symbol ordering is unknowable. Once latched, the daily stop does not auto-clear later that day.
4. **Boundary funding:** apply funding events due at `U` to positions that were open for that funding timestamp.
5. **Boundary protective/close-derived exits:** process session-end and confirmed false-break exits whose decision timestamp is `U`.
6. Reconcile released slots/reservations and recompute canonical/actual realized equity and daily realized PnL.
7. If a daily-loss stop or news cancellation became effective during the minute/boundary, cancel any still-unfilled pending entries. Fills already admitted by step 1 are not undone.

This portfolio-wide ordering intentionally resolves the ambiguity:

```text
pending entry B could fill in minute M
while position A loses enough in minute M to trigger the daily stop
```

as:

```text
B fill first → A loss/daily-stop latch → cancel remaining unfilled entries
```

This is the pessimistic ordering for unknown cross-symbol intraminute sequence.

### Activation batch at U

After the portfolio state above is final for `U`, gather every candidate with:

```text
entry_activation_time == U
```

Apply candidate-independent activation gates before ranking:

1. canonical decision data are available;
2. `U < order_expiry`;
3. `U` is not inside the news blackout;
4. neither canonical nor actual daily realized-loss stop is latched;
5. selected level has not already been consumed by an earlier successfully created entry;
6. activation-time PostOnly marketability gate passes.

Candidates failing any of these are rejected and never queued.

Only the surviving activation candidates are ranked.

When multiple survivors share the same signal/activation timestamp and capacity is insufficient, rank them by:

1. higher frozen daily 24h turnover;
2. higher frozen breakout volume_ratio;
3. symbol ascending as deterministic final tie-break.

Process survivors sequentially in that order.

For each survivor, using portfolio state **at activation time after all higher-ranked survivors already admitted in the same batch**:

1. compute `risk_equity`;
2. apply current slot, reserved-risk, daily-capacity, position-notional, and portfolio-notional limits;
3. calculate/floor quantity;
4. reject if exchange minimums fail;
5. create the canonical pending PostOnly LIMIT;
6. only after successful canonical entry-order creation:
   - consume the crossed objective levels;
   - reserve slot/risk/notional/daily capacity.

Rejected candidates are not queued for later admission.

A position that closes during `[T, T+60s)` may therefore free capacity for the activation batch at `T+60s`. Conversely, a loss during that minute may reduce equity or latch the daily stop before admission.

### Live exchange divergence during batch admission

If the canonical model admits a PostOnly order but the live exchange unexpectedly rejects/cancels it as marketable or for another exchange-state reason:

- log a parity deviation;
- do not promote a lower-ranked candidate that canonical ranking had rejected for capacity;
- do not use the unexpectedly freed actual slot/risk capacity for additional live risk;
- canonical shadow reservation/order state continues until the canonical order would cancel/expire or otherwise resolve.

This prevents favorable live/exchange discrepancies from increasing risk relative to the canonical backtest path.

---

## 15. Entry LIMIT and historical fill rule

Entry price is the normalized broken level.

Order lifetime is measured from deterministic activation time:

```text
signal_time = confirmed breakout 15m close
entry_activation_time = signal_time + 60 seconds

order_expiry =
min(
    signal_time + 120 minutes,
    originating_session_end
)
```

The live/demo bot must not place the entry LIMIT before `entry_activation_time`.

All canonical REST-1m acquisition, aggregation, and setup calculations must complete by that timestamp. If required 1m data are unavailable, reject with `REJECT_DECISION_DATA_UNAVAILABLE`.

This deliberate one-minute activation delay removes the unknowable partial-minute interval between a 15m close and real order acknowledgement from the 1m backtest.

The first historical 1m bar eligible to fill the LIMIT is the bar whose `open_time == entry_activation_time`.

### Activation-time PostOnly gate

The gate is evaluated inside the activation batch defined in Section 14, before capacity ranking.

Every live/demo entry order is:

```text
orderType = Limit
timeInForce = PostOnly
price = rounded entry_price
```

The strategy requires a **resting maker order**. It must never intentionally cross the spread and convert the retest entry into a taker fill.

Canonical historical activation price is:

```text
activation_open =
OPEN of the 1m last-price candle whose open_time == entry_activation_time
```

Reject before entry-order creation when:

```text
LONG:  activation_open <= entry_price
SHORT: activation_open >= entry_price
```

Reason:

```text
REJECT_ACTIVATION_THROUGH_LEVEL
```

This models a LIMIT that would already be marketable when submitted. Because no resting entry order was successfully created:

- crossed levels are not consumed;
- no pending-entry slot/risk/notional/daily-capacity reservation is created;
- the setup is not queued for a later retry.

Live exchange behavior is authoritative for actual execution. If Bybit cancels/rejects a PostOnly order because it would take liquidity, the real order remains unfilled.

Record parity telemetry for both mismatch directions:

```text
canonical ACCEPT, exchange PostOnly rejects
canonical REJECT, hypothetical live market state would have rested
```

The second case is telemetry/shadow-only because a canonically rejected order must not be submitted merely to test it.

A pre-activation excursion during `[signal_time, entry_activation_time)`, including touching the geometric stop, does **not** by itself reject the setup. There is no position before activation. Such excursions may be logged as diagnostics but cannot alter v0.2.5 decisions.

No chasing.

Historical execution uses 1m last-traded-price bars.

A touch is not a LIMIT fill.

LONG buy LIMIT fills only if:

```text
1m_low < entry_price
```

SHORT sell LIMIT fills only if:

```text
1m_high > entry_price
```

The strict inequality represents at least one actual market price increment through the order.

Baseline historical backtest assumes full fill once this conservative penetration rule is satisfied. Queue-position and partial-fill uncertainty are explicitly part of the execution limitations; live trading must handle real partial fills.

---

## 16. Entry-minute ambiguity

The 1m bar that first satisfies the entry fill condition is the **entry minute**.

Because OHLC cannot reveal the exact intraminute path:

- the position is considered entered during that minute;
- the initial stop may trigger in that same minute;
- no profit target may be credited in the same minute as entry.

If both entry and initial stop are reachable in the entry minute, count the stop after entry.

Even if TP1/TP2 are also inside that minute's range, they are ignored for that minute.

Profit-target eligibility begins with the next 1m bar.

This is intentionally pessimistic.

---

## 17. Stop, TP1, BE, and runner

Immediately after a live fill, protection must exist on the exchange, not only in process memory.

### Initial stop

A stop-market / conditional reduce-only protection is maintained for the actually filled quantity.

Both initial stop and cost-adjusted BE stop use:

```text
triggerBy = LastPrice
```

MarkPrice and IndexPrice triggers are forbidden in v0.2.5.

In the 1m backtest, stop triggering is based on last-traded-price OHLC and **touch is sufficient**:

```text
LONG stop:  1m_low  <= stop_price
SHORT stop: 1m_high >= stop_price
```

This asymmetry is intentional: LIMIT profit/entry fills require strict penetration, while an adverse stop requires only a touch.

### TP1

```text
50% of position at rounded 2 * R_price target
```

TP1 is a resting reduce-only LIMIT.

Backtest TP fill requires strict penetration beyond its price, not a touch.

### After TP1

After TP1 is filled:

- close the filled TP1 quantity;
- move protection for the remaining quantity to cost-adjusted BE;
- keep the slot and initial reservation occupied.

### TP2

If a valid next objective level exists, the remaining quantity uses that level as TP2.

If no next objective level exists, the runner remains until:

- false-break exit;
- BE/stop;
- end-of-day forced close.

### False breakout

False-break logic remains active until the position is completely closed, including after TP1.

LONG:

```text
confirmed 15m close < broken_level
→ market exit remaining position
```

SHORT:

```text
confirmed 15m close > broken_level
→ market exit remaining position
```

The market exit occurs only after the close is known.

### Live safety-first exits

Entry decisions wait for canonical REST-1m reconstruction until `T+60s`; protective exits do not.

In live/demo:

- a false-break exit may fire immediately from the native 15m WebSocket `confirm=true` close;
- the session-end exit fires immediately from the authoritative strategy clock at session end;
- neither waits for REST reconciliation.

The corresponding canonical REST-1m bar is reconstructed afterward and the difference is logged as post-hoc parity telemetry.

If a safety-first actual exit occurs before the canonical shadow path would exit, **do not reopen the real position**. The canonical shadow position/slot/reservation continues independently until its canonical exit so that an early live safety exit cannot free capacity and increase subsequent real risk relative to backtest.

For false-break and session-end market exits, the historical reference price is:

```text
open of the first 1m last-price candle whose open_time equals the decision timestamp
```

The corresponding execution-ledger slippage is then applied adversely to that reference price:

- LONG exit → price reduced by modeled slippage;
- SHORT exit → price increased by modeled slippage.

The canonical decision ledger uses DCM market-exit slippage; BASE/STRESS/SEVERE shadow ledgers use their own frozen execution assumptions without altering the trade path.

---

## 18. Intrabar priority for already-open positions

For each 1m bar, execution events that could have happened before the bar close are resolved before close-derived strategy decisions.

When OHLC cannot establish ordering, use the least favorable admissible sequence.

### Position existing at minute start

1. If the active stop at minute start and a profit target are both reachable, the stop is assumed first.
2. Otherwise TP1 may fill if its strict-penetration rule passes.
3. If TP1 creates a new BE stop and the same 1m range can hit that BE, assume the new BE is hit in the same minute.
4. If both the newly created BE and TP2 could occur after TP1 and order is unknowable, BE is assumed before TP2.
5. TP2 may fill only when no higher-priority adverse event is possible.

This rule extends the same pessimistic principle used for stop-vs-target ambiguity.

At a 15m boundary, intraminute executions are resolved first; then the newly closed 15m candle may trigger false-break logic.

---

## 19. Funding

Do not assume an 8-hour schedule.

Use the actual historical funding events for each symbol:

- fundingRateTimestamp;
- fundingRate.

Funding is applied only if the position is open at the actual funding timestamp.

For USDT perpetuals:

```text
funding_fee = quantity * mark_price_at_funding * funding_rate
```

Use Bybit historical Mark Price data, not Last Price, for the position-value term. The Data Contract requires 1m mark-price history around funding events.

Historical mark-price convention:

1. use the OPEN of the 1m Mark Price candle whose startTime equals fundingRateTimestamp;
2. if that candle is unavailable, use the CLOSE of the immediately preceding complete 1m Mark Price candle and flag MARK_PRICE_1M_APPROX;
3. if neither value is available, the affected trade is not parity-quality.

If funding eligibility is intraminute-ambiguous at the settlement boundary, apply a pessimistic rule: include the event when it is a payment by the strategy position, but do not credit it when it would be a receipt. Live actual funding is reconciled from exchange transaction records.

Funding contributes to realized net PnL and reported net R.

Development reporting must additionally include:

- count and share of positions crossing at least one funding event;
- funding paid/received in quote currency;
- funding as a fraction of gross trading PnL;
- funding contribution in net_R;
- distribution by funding interval/rate regime.

---

## 20. Daily loss limit

Historical/backtest:

```text
canonical_SOD_equity = canonical_realized_equity at 00:00 UTC
canonical_daily_stop = -1.0% * canonical_SOD_equity
```

Live maintains both:

```text
canonical_SOD_equity
actual_SOD_equity
```

and two realized daily PnL measures.

The live daily stop triggers if **either** condition is reached:

```text
canonical_daily_realized_pnl <= -0.01 * canonical_SOD_equity
OR
actual_daily_realized_pnl <= -0.01 * actual_SOD_equity
```

The first ledger to breach stops new risk.

Both ledgers include their respective realized fees and funding; unrealized PnL is excluded.

When reached:

- no new setup may be created;
- all pending entry LIMITs are cancelled;
- open positions continue to be managed by their existing strategy rules.

The daily stop does not force-close existing positions.

---

## 21. Net-R reporting

Never report gross R as the primary result.

For each setup, record the actual initial planned monetary risk after all sizing caps:

```text
planned_risk_usd = actual_qty * L_unit
```

Then:

```text
net_R =
realized_PnL_after_fees_slippage_funding
/
planned_risk_usd
```

If quantity was reduced by notional caps, the denominator is reduced to the actual risk taken.

Required reports include:

- gross price-R;
- net R;
- fees;
- modeled/realized slippage;
- funding;
- intended risk;
- actual risk;
- whether notional cap reduced size.

---

## 22. Development funnel

Before any validation-period results are opened, the development period must produce a complete signal funnel.

At minimum count:

```text
eligible universe observations
→ directional level crossings
→ trend PASS
→ volume PASS
→ session PASS
→ news PASS
→ cost-floor PASS
→ room PASS
→ capacity PASS
→ LIMIT created
→ LIMIT filled
→ completed trades
```

For every rejection stage, report count and percentage.

Also report:

- R_price distribution;
- C_stop / R_price distribution;
- volume_ratio by local time-of-day;
- decision-data-unavailable count/rate in live/shadow telemetry;
- percentage of setups clipped by risk-capacity and position/portfolio notional caps;
- count/share of trades crossing funding events and funding contribution to net_R;
- annualized filled-trade count.

The purpose is to detect strategy starvation—especially from the cost floor—before opening validation data.

Low projected sample size may motivate a new specification version **only while still inside the development phase**. It may not justify changing v0.2.5 after validation/holdout inspection.

---

## 23. Non-negotiable implementation invariants

1. No future candle or future instrument membership may be visible to a decision.
2. 1H context always means the latest fully closed 1H bar.
3. The breakout volume average excludes the breakout candle.
4. Dynamic universe is reconstructed as-of each historical day.
5. Backtest and live share the same strategy decision code.
6. Backtest and live use the same price-grid normalization rules.
7. Historical fills are never granted from a mere touch.
8. Ambiguous OHLC ordering is pessimistic.
9. Pending orders reserve risk and capacity before fill.
10. Stops for live positions reside on the exchange.
11. Reconnection always reconciles orders/positions with exchange state.
12. Strategy behavior cannot change without a new versioned specification.
13. Decision Cost Model values never auto-update from live fee APIs.
14. Initial/BE stop trigger type is always LastPrice.
15. Canonical `close_time = start_time + interval` and bars are half-open.
16. Entry LIMIT activation is exactly one minute after the breakout close.
17. Every live/demo entry LIMIT is PostOnly and canonical activation rejects marketable orders with `REJECT_ACTIVATION_THROUGH_LEVEL`.
18. Entry decisions use locally aggregated REST 1m bars; native WS/15m REST differences are telemetry, not a selection filter.
19. Live sizing uses `min(canonical_realized_equity, actual_realized_equity)`.
20. Either canonical or actual live daily-loss breach blocks new risk.
21. Indicator chains re-seed after unresolved data gaps using the exact rules above.

See [VALIDATION_PROTOCOL_V0.2.2.md](VALIDATION_PROTOCOL_V0.2.2.md) for frozen sample boundaries and PASS/FAIL/INCONCLUSIVE rules.
