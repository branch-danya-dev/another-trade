# another-trade

A new intraday trading-system project built around one deterministic strategy:

**trend-following breakout → retest of objective higher-timeframe levels on Bybit USDT perpetuals.**

This repository intentionally does **not** inherit trading logic, strategy engines, gates, heuristics, or architecture from previous bot projects.

## Current status

- Trading specification: **v0.2.1**
- Specification state: **frozen for implementation**
- Next phase: **historical data audit / Data Contract validation**
- Backtest implementation: not started
- Demo/live implementation: not started

The first goal is not to prove profitability. The first goal is to build a reproducible system in which the same market information produces the same decision in backtest, demo, and live modes.

## Documents

- [Trading specification v0.2.1](docs/SPEC_V0.2.1.md)
- [Archived specification v0.2](docs/SPEC_V0.2.md)
- [Data Contract](docs/DATA_CONTRACT.md)
- [Validation protocol](docs/VALIDATION_PROTOCOL.md)
- [Live operations & compliance gate](docs/LIVE_OPERATIONS_AND_COMPLIANCE.md)
- [Agent instructions](agent.md)

## Core principles

1. Only objective levels: PDH, PDL, PWH, PWL.
2. LONG breakouts are allowed only through PDH/PWH. SHORT breakouts only through PDL/PWL.
3. Decisions use only closed candles.
4. Strategy timeframe: 15m / 1H; execution resolution for backtest: 1m.
5. Limit fills require strict penetration, not a touch.
6. Ambiguous intrabar execution is resolved pessimistically.
7. All performance is reported after commissions, slippage, and funding.
8. Risk is sized from expected all-in loss, not raw stop distance.
9. Pending entries reserve position slots, risk, notional, and daily trade capacity.
10. Development, validation, and final holdout periods are separated before testing.
11. No parameter is changed after seeing holdout results without creating a new strategy version.
12. Demo/live must reuse the same strategy core as the backtester.

## Planned workflow

```text
Data audit
  ↓
canonical historical dataset
  ↓
deterministic event-driven backtester
  ↓
development period analysis
  ↓
freeze
  ↓
validation
  ↓
final holdout
  ↓
demo forward test
  ↓
minimal-size real trading only after protocol PASS
```

## Exchange / external data

Primary market venue: Bybit V5.

Planned data classes:

- instrument universe and contract metadata;
- 1m historical klines;
- derived 15m / 1H / daily / weekly bars;
- funding history;
- official U.S. CPI and Employment Situation release schedules from BLS;
- FOMC meeting / statement calendar from the Federal Reserve.

See [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md) for the audit requirements and known limitations.
