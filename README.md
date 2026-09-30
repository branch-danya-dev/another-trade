# another-trade

A new intraday trading-system project built around one deterministic strategy:

**trend-following breakout → retest of objective higher-timeframe levels on Bybit USDT perpetuals.**

This repository intentionally does **not** inherit trading logic, strategy engines, gates, heuristics, or architecture from previous bot projects.

## Current status

- Trading specification: **v0.2.4**
- Specification state: **frozen for implementation**
- Next phase: **historical data audit / Data Contract validation**
- Backtest implementation: not started
- Demo/live implementation: not started

The first goal is not to prove profitability. The first goal is to build a reproducible system in which the same market information produces the same decision in backtest, demo, and live modes.

## Documents

- [Trading specification v0.2.4](docs/SPEC_V0.2.4.md)
- [Data Contract v0.1.6](docs/DATA_CONTRACT_V0.1.6.md)
- [Validation protocol v0.2.5](docs/VALIDATION_PROTOCOL_V0.2.5.md)
- [Live operations & compliance gate](docs/LIVE_OPERATIONS_AND_COMPLIANCE.md)
- [Archived specification v0.2.1](docs/SPEC_V0.2.1.md)
- [Archived specification v0.2](docs/SPEC_V0.2.md)
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
10. Decision Cost Model is frozen separately from BASE/STRESS/SEVERE execution ledgers.
11. Canonical bars use half-open time intervals and `close_time = start + interval`.
12. Live entry decisions are aggregated from REST 1m data; WebSocket/native higher-timeframe candles are telemetry only.
13. Live sizing uses the lower of canonical and actual realized equity; either live daily-loss ledger can halt new risk.
14. Development, validation, and final holdout periods are separated before testing.
15. No parameter is changed after seeing holdout results without creating a new strategy version.
16. Demo/live must reuse the same strategy core as the backtester.

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
pre-live operations / compliance gate
  ↓
minimal-size real trading only after protocol PASS + deployment gate PASS
```

## Exchange / external data

Primary market venue: Bybit V5.

Planned data classes:

- instrument universe and contract metadata;
- 1m historical klines;
- derived 15m / 1H / daily / weekly bars;
- funding history and 1m Mark Price history;
- official U.S. CPI and Employment Situation release schedules from BLS;
- FOMC meeting / statement calendar from the Federal Reserve.

See [docs/DATA_CONTRACT_V0.1.6.md](docs/DATA_CONTRACT_V0.1.6.md) for the audit requirements and known limitations.


## Data Audit milestone 1

Install:

```bash
python -m pip install -e .
python -m pip install pytest mypy ruff
```

Capture the required **real mainnet Bybit fixtures** once from a network where Bybit public API access is permitted:

```bash
python scripts/capture_bybit_fixtures.py
```

The capture stores raw response bytes and SHA-256 hashes under `tests/fixtures/bybit/`. Do not replace these fixtures with invented JSON or testnet data.

Then run:

```bash
ruff check .
mypy src
pytest
another-trade audit inventory
another-trade audit sample-coverage
```

`sample-coverage` is intentionally not a bulk downloader. It derives `first_trade_ms`, performs aligned control probes (including a pre-launch lifetime-conflict probe) plus one bounded funding probe, checkpoints each symbol to JSONL, and writes a run manifest. Review the run-specific `coverage-summary.json` before implementing any full-history download.

GitHub-hosted runners currently execute from a Bybit-restricted U.S. region, so CI validates code without requiring network access to Bybit. Real fixtures and the first sample-coverage run must be captured from an allowed network.
