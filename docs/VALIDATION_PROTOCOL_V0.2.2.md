# Validation Protocol v0.2.2

Status: **PRE-REGISTERED**

This document defines when strategy v0.2.2 is considered PASS, FAIL, or INCONCLUSIVE.

The purpose is to prevent repeated historical testing from turning validation data into another development set.

Canonical strategy: [SPEC_V0.2.2.md](SPEC_V0.2.2.md).

---

## 1. Frozen time partitions

All boundaries are UTC and half-open: [start, end).

### Development

```text
2022-01-01 00:00:00
→
2025-01-01 00:00:00
```

Allowed uses:

- implementation debugging;
- data-pipeline debugging;
- execution-model calibration;
- parameter sensitivity analysis;
- funnel analysis;
- changing the strategy specification.

Any strategy rule may change while only development data have been inspected, but such a change requires a new explicit spec version before validation.

### Validation

```text
2025-01-01 00:00:00
→
2026-01-01 00:00:00
```

Validation may be opened only after:

- SPEC_V0.2.2 is frozen;
- [DATA_CONTRACT_V0.1.3.md](DATA_CONTRACT_V0.1.3.md) audit is complete enough for the tested universe;
- development-only DCM-independent public-trade slippage calibration artifact is complete;
- Decision Cost Model and BASE/STRESS/SEVERE execution models are frozen from development evidence;
- baseline and robustness neighbors are frozen;
- code tests pass;
- the development funnel report is archived.

If strategy behavior changes after validation is opened, 2025 is permanently contaminated for that new version and may no longer be described as unseen OOS.

### Minimum validation sample

The 2025 validation partition has a pre-registered minimum:

```text
N_2025 >= 100 filled setups
```

If `N_2025 < 100`:

- if any mandatory directional/economic point estimate is non-positive, the result is `FAIL_VALIDATION`;
- otherwise the result is `INCONCLUSIVE_VALIDATION`;
- the 2026 final holdout remains sealed.

There is no subjective "obviously too small" exception.

### Final holdout

```text
2026-01-01 00:00:00
→
2026-09-30 00:00:00
```

The end is deliberately the last fully completed UTC day before this protocol was frozen; no partial 2026-09-30 session is included.

Final holdout must not be queried for strategy PnL, setup counts, parameter comparisons, or trade-level inspection before the validation gate passes.

If v0.2.2 changes after final holdout is opened, the project has no remaining clean historical holdout for the new version.

---

## 2. Baseline parameters

Frozen v0.2.2 baseline:

```text
EMA period                 50
EMA slope lookback          3 closed 1H bars
breakout volume ratio       1.5
ATR period                 14
stop distance               0.5 * ATR15
retest expiry               8 * 15m bars, session-capped
cost-floor k                5
risk per trade              0.5% realized equity
max active slots            2
max filled setups/day       3
daily realized loss stop    1.0% SOD equity
position notional cap       2.0x realized equity
portfolio notional cap      3.0x realized equity
portfolio reserved risk     1.0% realized equity
TP1                         50% at 2 R_price
```

The Decision Cost Model is stored under an explicit immutable config version. BASE/STRESS/SEVERE execution-model parameters are separate versioned configs. All must be frozen before validation.

Slippage calibration must follow DATA_CONTRACT_V0.1.3 using development-period official public-trade archives only:

```text
DCM decision slippage → development p75
BASE                 → development p75
STRESS               → development p95
SEVERE               → development p99
```

using the pre-declared regime/bucket fallback rules.

Changing live/account fee assumptions must never silently alter the Decision Cost Model.

---

## 3. Pre-registered robustness neighbors

Robustness uses one-factor-at-a-time neighbors. Do not search the Cartesian product.

Ten neighbors:

```text
EMA45
EMA55

volume_ratio_1.4
volume_ratio_1.6

ATR_stop_0.4
ATR_stop_0.6

expiry_6
expiry_10

cost_floor_4
cost_floor_6
```

Every non-mentioned parameter stays at baseline.

No new neighbor may be added after validation results are visible.

---

## 4. Development funnel gate

Before opening validation, produce the funnel defined in SPEC_V0.2.2.

Mandatory additional estimate:

```text
dev_filled_trades_per_calendar_year
projected_OOS_filled_trades
```

The projection must be mechanical and clearly labeled as a rough sample-size estimate, not a performance forecast.

If projected combined OOS N is below 200, there are only two acceptable choices before opening validation:

1. redesign/version the strategy using development data only; or
2. freeze v0.2.2 unchanged and explicitly accept that the historical result is likely to become INCONCLUSIVE.

It is forbidden to open validation, discover insufficient N, then loosen filters to manufacture more trades under the same version.

---

## 5. Validation unlock gate

Run baseline plus the ten frozen neighbors on 2025 only.

The final 2026 holdout remains sealed.

Validation gate passes only if all of the following are true:

1. `N_2025 >= 100` filled setups;
2. baseline 2025 mean net_R per filled setup > 0;
3. baseline 2025 total net PnL > 0;
4. baseline STRESS shadow-execution 2025 mean net_R >= 0;
5. baseline STRESS shadow-execution 2025 total net PnL >= 0;
6. at least 8 of 10 frozen neighboring configurations have positive mean net_R in 2025;
7. no discovered data or code defect invalidates the run.

BASE/STRESS/SEVERE must consume the same canonical decisions, quantities, fills/triggers, and trade membership. Scenario PnL may not feed back into later strategy decisions.

If any mandatory baseline or stress point estimate is negative/non-positive where the condition above requires strict positivity/non-negativity, v0.2.2 is **FAIL_VALIDATION** and final holdout stays sealed.

If all required point-estimate conditions pass but `N_2025 < 100`, the result is **INCONCLUSIVE_VALIDATION** and final holdout stays sealed.

No parameter may be selected because it beat the baseline in validation.

The baseline remains the baseline.

---

## 6. Final holdout run

After validation unlock passes:

- run the frozen baseline on 2026 final holdout exactly once for decision purposes;
- run the frozen BASE and STRESS execution scenarios;
- do not tune parameters from final-holdout results;
- archive the exact code commit, dataset manifest, execution-model version, and config hash.

The ten parameter neighbors are **not** re-optimized on the final holdout.

Robustness evidence comes from the pre-registered 2025 validation run, preserving the final holdout primarily for baseline confirmation.

---

## 7. Combined OOS statistics

After the final holdout is legitimately opened, combined OOS means:

```text
2025 validation
+
2026 final holdout
```

Required metrics:

- N filled setups;
- mean net_R;
- median net_R;
- total net_R;
- total net PnL;
- win rate;
- average win net_R;
- average loss net_R;
- profit factor;
- maximum drawdown;
- maximum losing streak;
- fees / gross PnL;
- slippage / gross PnL;
- funding / gross PnL;
- results by session;
- results by symbol;
- results by source level type;
- results by calendar month;
- capacity-rejection count;
- cost-floor rejection count;
- slippage calibration bucket/fallback provenance;
- actual-vs-DCM fee/slippage telemetry when forward/live evidence is later collected.

Primary expectancy metric:

```text
mean net_R per filled setup
```

---

## 8. Weekly block bootstrap

Do not use IID trade bootstrap.

Bootstrap unit: complete UTC strategy weeks:

```text
Monday 00:00 UTC
→ next Monday 00:00 UTC
```

Weeks are sampled with replacement and their contained trades remain grouped.

Use a deterministic published seed and at least 20,000 bootstrap resamples.

For each resample:

1. sample the same number of weekly blocks as in the observed OOS interval;
2. concatenate trades contained in those sampled weeks;
3. compute mean net_R per trade;
4. record the result.

Primary confidence interval:

```text
percentile 95% CI
2.5th percentile → 97.5th percentile
```

Weeks with no trades remain legitimate blocks; they are not deleted from the calendar before resampling.

The exact bootstrap implementation, seed, and library versions must be stored with the run.

---

## 9. Final PASS

v0.2.2 receives historical **PASS** only if all conditions below are true:

### Sample size

```text
combined OOS N >= 200 filled setups
```

### Directional consistency

```text
2025 baseline mean net_R > 0
2026 holdout baseline mean net_R > 0
combined OOS baseline mean net_R > 0
```

### Monetary consistency

```text
2025 baseline total net PnL > 0
2026 holdout baseline total net PnL > 0
combined baseline total net PnL > 0
```

### Execution stress

```text
combined OOS STRESS shadow-ledger mean net_R >= 0
combined OOS STRESS shadow-ledger total net PnL >= 0
```

### Statistical evidence

```text
weekly-block-bootstrap 95% CI lower bound for mean net_R > 0
```

### Robustness

The frozen 2025 robustness gate already passed:

```text
>= 8 of 10 one-factor neighbors positive
```

### Integrity

No unresolved data-quality or implementation defect invalidates the sampled trades.

All conditions are conjunctive.

---

## 10. FAIL

A result is **FAIL** when evidence contradicts the frozen economic hypothesis rather than merely being imprecise.

Examples:

- validation baseline mean net_R <= 0;
- final holdout baseline mean net_R <= 0;
- combined OOS baseline mean net_R <= 0;
- combined STRESS mean net_R < 0;
- combined total net PnL <= 0;
- robustness gate < 8/10 positive neighbors;
- material data bias invalidates the test and cannot be repaired without changing the evaluated sample.

After FAIL, do not tweak v0.2.2 and rerun the same OOS as if it were unseen.

Any redesigned strategy becomes a new version.

---

## 11. INCONCLUSIVE

A result is **INCONCLUSIVE**, not FAIL, only when every point-estimate sign requirement already passes but evidence is insufficient.

Primary cases:

1. `N_2025 < 100` while all mandatory 2025 baseline/stress point estimates satisfy their required signs;
2. combined OOS N < 200 while all mandatory baseline/stress point estimates satisfy their required signs; or
3. weekly block-bootstrap 95% CI lower bound <= 0 while baseline mean net_R and total net PnL remain strictly positive and STRESS mean net_R / total net PnL remain non-negative; or
4. data coverage is adequate for directional research but insufficient for a parity-quality claim.

A non-positive baseline mean net_R or total net PnL is never reclassified as INCONCLUSIVE merely because N is small.

INCONCLUSIVE must not trigger parameter changes based on OOS observations.

---

## 12. What to do after INCONCLUSIVE

Freeze the strategy and execution model unchanged.

Proceed to forward collection using current live market data:

- strategy decisions generated from the same production strategy core;
- shadow execution recorded against real mainnet market data;
- demo orders may run in parallel to verify exchange integration;
- no discretionary trade removal;
- no parameter change.

Forward evidence is reported separately from historical OOS.

A forward result may be assessed with the same philosophy:

- positive net expectancy;
- positive total net PnL;
- non-negative stress shadow result;
- weekly-block bootstrap;
- sufficient filled setups for the pre-declared statistical test.

Do not merge demo execution quality blindly with historical simulated fills. Maintain separate statistics and a clearly defined combined-evidence report if one is later approved.

If rules change during forward collection, the forward sample collected before the change belongs to the old version.

---

## 13. Stress and severe scenarios

At least three execution scenarios must exist before validation:

### BASE

Best conservative estimate intended to represent expected live costs.

### STRESS

Plausibly adverse fees/slippage assumptions, including time-dependent news slippage for market exits inside official event time ±5 minutes.

STRESS is part of PASS/FAIL.

### SEVERE

Tail diagnostic used to understand failure behavior.

SEVERE is reported but is not itself a PASS gate in v0.2.

Numerical scenario parameters must be frozen from development evidence / conservative assumptions and committed before validation unlock.

---

## 14. No OOS-driven exception handling

Forbidden after validation unlock:

- changing cost-floor k because trade count is low;
- removing a bad symbol after seeing its OOS losses;
- changing session times after observing OOS performance;
- changing breakout direction rules;
- changing level merge behavior;
- changing intrabar priority;
- changing news rules;
- changing slippage only for trades that performed badly;
- choosing a neighboring parameter because it scored better than baseline.

Bug fixes are allowed only when they restore the already-written specification.

A bug fix requires:

1. issue description;
2. failing test demonstrating the discrepancy;
3. fix;
4. impact analysis identifying which historical results are invalidated;
5. rerun under the same frozen spec.

---

## 15. Required run manifest

Every development, validation, or holdout result must persist:

```text
run_id
run_type
spec_version
decision_cost_model_version
execution_model_version
git_commit_sha
dataset_version
dataset_manifest_hash
config_hash
start_time
end_time
symbols_considered
bootstrap_seed
software_versions
result_artifact_hashes
```

A result without a reproducible manifest is diagnostic only and cannot satisfy a PASS gate.
