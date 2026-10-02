# Pre-registration: ORB V1

Frozen on 2026-10-02, **before any real-data run**. Results are judged only
against what is written here.

## Identity

| | |
|---|---|
| Spec version | **V1** (the original spec plus `docs/DECISIONS.md` items 1–57) |
| Code commit | `8577adff375d398451b51d475e19db602fa4368a` |
| Config hash | `0822eb04daa9cd48e788a8bd75152743a77a2cf6931e884dfd144e0af62eea0b` (`config/default.yaml` + tick and cost tables, including the `prereg` thresholds) |
| In-sample period | 2018-01-01 to 2024-09-30 |
| OOS start | 2024-10-01 (OOS end: the last date of the frozen data snapshot, at most 2026-09-30) |
| Data | Each run records its data version: the frozen Kite snapshot hash, the raw store and the reference files. |

## What is evaluated

- **Primary book:** the default entry-candle rule (no stop check on the entry
  candle) at 1× slippage, with STT and stamp duty rounded to the rupee per day
  (`net_pnl_rounded`). The conservative entry-candle variant and the 2×/3×
  slippage books are reported alongside but don't decide anything, except
  criterion b's 2× check.
- **Null (criterion c):** each taken trade against a twin with only the
  direction flipped: same entry candle, same quantity, same rupee costs, stop
  mirrored at the same per-share distance, same exit rules. 2,000 fair-coin
  draws with seed 20180101; one-sided p = P(random total ≥ actual total).
  Decision 55 has the details. The strict sign-flip at the actual exit time is
  reported as a secondary null only.

## Success criteria (in-sample)

| | Criterion | Measured on |
|---|---|---|
| a | ≥ 300 taken trades | primary book |
| b | net P&L > 0 at 1× slippage **and** ≥ 0 at 2× slippage, both with rupee rounding | default variant, 1× and 2× books |
| c | beats the random-direction null at p < 0.05 | primary book |
| d | net P&L > 0 in ≥ 60% of calendar years (years with at least one trade) | primary book |
| e | **score usefulness:** mean net R per trade rises strictly across the 65–74, 75–84 and 85+ score buckets | labels: every scored first breakout with score ≥ 65, simulated as if taken (default variant, 1×), excluding EXCLUDED stock-days |

**Gate:** a–d must all pass in-sample. Criterion e doesn't gate. If e fails,
the score is judged not useful and V1 is evaluated as a plain ORB (any
qualifying breakout); its results are still reported.

`orb report` prints one `[PASS]`/`[FAIL]` line per criterion, and the gate
verdict, directly under the survivorship-gap line of `report.md`. The
thresholds live in `config/default.yaml` under `prereg` and are covered by the
config hash above.

## Out-of-sample

- OOS is run **once**, and only if a–d pass in-sample. The engine refuses OOS
  without `--oos` and records every OOS run in `data/_runs/oos_ledger.jsonl`;
  a rerun needs a logged reason.
- The OOS verdict uses the same criteria **b–d**. Criterion a doesn't apply
  out of sample.
- The OOS run must use the exact code commit and config hash of the accepted
  in-sample run.

## Data preconditions (before the in-sample run)

1. The Kite download is frozen as a snapshot (`orb snapshot freeze`), and
   `build-raw` has been run from it.
2. The `needs_review=true` rows in `data/ref/manual/nifty200_changes_manual.csv`
   have been reviewed.
3. `data/ref/manual/mergers.csv` has been reviewed (only reviewed rows apply).
4. `orb dq` has been run. Its survivorship gap is reported, not used as a gate.

## Changes after results

- **Bug fixes only.** Each one is logged in `docs/CHANGELOG_RESEARCH.md` with
  the date, the reason, and before/after metrics for every criterion.
- **Any change to the spec** (rules, parameters, filters, scoring, sizing,
  universe definition) creates a new version, **V1.1**. It needs its own
  pre-registration and its own new OOS test. The V1 OOS result stands as
  reported.

## Amendments (all made before any real-data run)

### 2026-10-02: secondary null is diagnostic only

- Criterion c is decided **only** by the mirrored-stop null described above.
- The strict sign flip at the actual exit time (−gross − costs per trade, same
  coin-flip procedure) is reported in `report.md` under "Secondary diagnostic"
  and is **explicitly excluded from pass/fail** for every criterion, in-sample
  and OOS.
- No config value changed. The config hash stays
  `0822eb04daa9cd48e788a8bd75152743a77a2cf6931e884dfd144e0af62eea0b`.

