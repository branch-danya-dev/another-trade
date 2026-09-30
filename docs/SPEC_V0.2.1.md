# Trading Specification v0.2.1

Status: **FROZEN FOR IMPLEMENTATION**

Supersedes v0.2 before validation was opened. v0.2 remains archived for traceability.

This document is the canonical trading specification. Code must implement it literally. Any behavioral change requires a new specification version before implementation.

## 1. Scope

One strategy only:

**intraday trend-following breakout and retest of objective previous-period levels.**

No discretionary level quality, order-flow score, manual override, ML prediction, density confirmation, round-number heuristic, local pivot clustering, or inherited logic from previous trading-bot projects is part of v0.2.

Market: Bybit USDT linear perpetuals.

BTC is context only and is not traded by v0.2.

---

## 2. Time model

All stored timestamps are UTC.

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

Only 15m bars with `close_time < universe_cutoff` may enter universe ranking. Therefore the last eligible 15m bar is the one closing at 08:45 Europe/Amsterdam. The first Europe-session breakout candle cannot influence its own universe membership.

Eligible instruments must satisfy all of the following as-of the historical selection timestamp:

- Bybit category = `linear`;
- `contractType = LinearPerpetual`;
- `quoteCoin = USDT`;
- `settleCoin = USDT` where the field is available;
- `isPreListing = false`;
- `marketRegion` is empty / not applicable, excluding TradFi perpetuals with external-market sessions;
- status/lifetime data prove the instrument existed and was in normal Trading state at that historical time;
- no expiry/futures contract is admitted;
- sufficient prior data exist to compute all required levels, EMA, ATR, volume ratio, turnover, and volatility;
- BTCUSDT is excluded from the tradable universe.

Pre-market contracts, TradFi perpetuals, delivery futures, event contracts, and other non-crypto derivatives are not eligible.

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

Historical spread is not a strategy filter in v0.2.

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

Those are different patterns and are outside v0.2.

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

### Live closed-candle reconciliation

A WebSocket candle with `confirm=true` is the live close notification, but it is not by itself sufficient for a new entry decision.

At each 15m breakout decision boundary:

1. capture and persist the confirmed WebSocket candle;
2. fetch the same closed 15m candle via REST;
3. require exact normalized OHLCV/turnover equality after Decimal parsing and exchange-grid normalization;
4. if REST data are not available or do not reconcile by `breakout_close + 60 seconds`, reject the setup as `REJECT_DATA_RECONCILIATION`.

The system logs every WS↔REST comparison, including exact field deltas.

This one-minute reconciliation window also defines deterministic entry activation; see Section 15.

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

The volume ratio must be logged with local time-of-day for later diagnostics. v0.2 does not normalize volume by time-of-day.

---

## 7. News blackout

Blocked U.S. macro events:

- CPI;
- Employment Situation / NFP;
- FOMC policy decision;
- FOMC press conference.

Blackout:

```text
event_time - 30 minutes
through
event_time + 30 minutes
```

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

After price rounding:

```text
R_price = abs(entry_price - stop_price)
```

R_price is the strategy's geometric price-risk unit.

It is distinct from all-in monetary risk and from reported net R.

---

## 9. Price-grid rules

All calculations use exact decimal arithmetic, never binary floating-point rounding.

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

Capital basis:

```text
realized_equity =
initial_equity
+ realized closed trading PnL
- realized fees
+/- realized funding
```

Unrealized PnL is excluded.

Per-trade risk budget:

```text
risk_budget = realized_equity * 0.005
```

Risk-sized quantity:

```text
qty_risk = risk_budget / L_unit
```

Then apply portfolio-risk and notional caps:

```text
max_total_reserved_risk = 0.01 * realized_equity
remaining_risk_capacity =
    max(0, max_total_reserved_risk - sum(existing_initial_risk_reservations))

qty_risk_capacity =
    remaining_risk_capacity / L_unit

max_position_notional = 2.0 * realized_equity
max_total_reserved_notional = 3.0 * realized_equity
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
max_total_reserved_risk = 1.0% of current realized equity
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

## 14. Candidate ranking when capacity is contested

Signals are processed in chronological decision-time order. Earlier confirmed 15m signals naturally acquire capacity first.

When multiple candidates share the exact same decision timestamp and available capacity is insufficient, rank them by:

1. higher frozen daily 24h turnover;
2. higher breakout volume_ratio;
3. symbol ascending as deterministic final tie-break.

Rejected candidates are not queued for later admission.

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

The live/demo bot must not place the entry LIMIT before `entry_activation_time`. All candle reconciliation and setup calculations must complete before that timestamp; otherwise reject the setup.

This deliberate one-minute activation delay removes the unknowable partial-minute interval between a 15m close and real order acknowledgement from the 1m backtest.

The first historical 1m bar eligible to fill the LIMIT is the bar whose `open_time == entry_activation_time`.

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

MarkPrice and IndexPrice triggers are forbidden in v0.2.1.

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

If exact tick-level mark price at the funding timestamp is unavailable, use the 1m Mark Price candle value defined by the Data Contract and flag the approximation explicitly.

Funding contributes to realized net PnL and reported net R.

Development reporting must additionally include:

- count and share of positions crossing at least one funding event;
- funding paid/received in quote currency;
- funding as a fraction of gross trading PnL;
- funding contribution in net_R;
- distribution by funding interval/rate regime.

---

## 20. Daily loss limit

Start-of-day equity:

```text
SOD_equity = realized_equity at 00:00 UTC
```

Daily loss stop:

```text
-1.0% of SOD_equity
```

It is evaluated using realized net PnL after fees, slippage, and funding.

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
- percentage of setups clipped by risk-capacity and position/portfolio notional caps;
- count/share of trades crossing funding events and funding contribution to net_R;
- annualized filled-trade count.

The purpose is to detect strategy starvation—especially from the cost floor—before opening validation data.

Low projected sample size may motivate a new specification version **only while still inside the development phase**. It may not justify changing v0.2 after validation/holdout inspection.

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
15. Entry LIMIT activation is exactly one minute after the breakout close.
16. WS-confirmed decision candles must reconcile against REST before entry activation.

See [VALIDATION_PROTOCOL.md](VALIDATION_PROTOCOL.md) for frozen sample boundaries and PASS/FAIL/INCONCLUSIVE rules.
