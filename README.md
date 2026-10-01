# another-trade

A new intraday trading-system project built around one deterministic strategy:

**trend-following breakout → retest of objective higher-timeframe levels on Bybit USDT perpetuals.**

This repository intentionally does **not** inherit trading logic, strategy engines, gates, heuristics, or architecture from previous bot projects.

## Current status

- Trading specification: **v0.2.6**
- Specification state: **frozen for implementation**
- Next phase: **historical data audit / Data Contract validation**
- Backtest implementation: not started
- Demo/live implementation: not started

The first goal is not to prove profitability. The first goal is to build a reproducible system in which the same market information produces the same decision in backtest, demo, and live modes.

## Documents

- [Trading specification v0.2.6](docs/SPEC_V0.2.6.md)
- [Data Contract v0.1.12](docs/DATA_CONTRACT_V0.1.12.md)
- [Validation protocol v0.2.11](docs/VALIDATION_PROTOCOL_V0.2.11.md)
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

See [docs/DATA_CONTRACT_V0.1.12.md](docs/DATA_CONTRACT_V0.1.12.md) for the audit requirements and known limitations.


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


## Bulk pilot

Before full historical download, run the bounded development-period pilot:

```bash
another-trade audit bulk-pilot --month 2024-06
```

To exercise crash/resume deliberately:

```bash
another-trade audit bulk-pilot --month 2024-06 --abort-after-pages 12
# copy the printed run directory, then:
another-trade audit bulk-pilot --month 2024-06 --resume-run "<run-dir>"
```

The pilot stores raw Bybit responses in one transactional SQLite container per symbol/month, writes canonical Parquet with pinned `pyarrow==25.0.1`, compares local 1m aggregation against native 15m/1H/D bars, and tests 1m vs 60m Mark Price OPEN at funding timestamps.


### Pre-full-download reproducibility checks

After the resumed pilot, run an independent clean pilot under the same commit/config, then compare:

```bash
another-trade audit bulk-pilot --month 2024-06
another-trade audit bulk-pilot-compare \
  --reference-run "<resumed-run>" \
  --candidate-run "<clean-run>"
```

Also run the lifetime-boundary pilot:

```bash
another-trade audit bulk-boundary-pilot --month 2024-09
```

The full universe download is blocked until the clean logical hashes match and the MATIC/POL partial-month partition test passes.


### Final targeted gates before full download

Historical fast-funding / Mark Price equivalence:

```bash
another-trade audit funding-mark-pilot
```

This probes development-period symbol-months selected for historically observed short funding cadence and verifies every funding timestamp against exact 1m and 60m Mark Price OPEN while retaining raw evidence and semantic payload hashes.

Refresh the migration/delivery boundary pilot with funding and Mark Price evidence:

```bash
another-trade audit bulk-boundary-pilot --month 2024-09
```

Full-universe download remains blocked until both targeted gates pass.


## Full historical download

The pilot gate is closed. The canonical full download is:

```bash
another-trade audit bulk-download
```

The run freezes the current inventory, discovers/resumes historical lifetimes, creates an oldest-first symbol/month plan, stores canonical 1m + funding + hourly Mark Price + delivery Index Price evidence, and computes structural integrity only.

For a two-partition smoke test before committing to the long run:

```bash
another-trade audit bulk-download --max-partitions 2
```

Then continue the exact same run:

```bash
another-trade audit bulk-download --resume-run "<run-dir>"
```

By default OPEN partitions are skipped. On 2026-10-01 the September 2026 partition remains pending until 2026-10-02 00:00 UTC; resume the same run after it seals.
