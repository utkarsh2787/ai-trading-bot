# Pre-registration: ORB V1

Frozen on 2026-10-02, **before any real-data run**. Results are judged only
against what is written here.

## Identity

| | |
|---|---|
| Spec version | **V1** (the original spec plus `docs/DECISIONS.md` items 1–57) |
| Code commit | **pinned automatically by the first in-sample run**, in its `meta.json` and in `data/_runs/insample_pin.json` (amendment of 2026-10-02, "code-provenance rule"). The earlier manual pins (`8577adf` and later) are superseded. |
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

### 2026-10-02: code commit re-pinned (still before any real-data run)

- **Code commit is now `c3e9f3d2b8f0bd8217fcde07f1bbf65aef8e6912`** (it replaces `8577adf`). Commits in between:
  - merger candidate pre-fill and Nifty 200 exit dates (reference tooling);
  - the daily Nifty 200 count check;
  - Kite login, `download --plan` and coverage (download tooling);
  - special-session detection, and weekend bhavcopies;
  - **one change that affects computed results:** daily bars on excluded
    whole-market days (Muhurat, DR or mock sessions) no longer feed the
    ATR14 history (decision 59).
- No criterion, threshold or config value changed; the config hash is still
  `0822eb04daa9cd48e788a8bd75152743a77a2cf6931e884dfd144e0af62eea0b`.
- Before the first in-sample run, the run's `meta.json` git commit must equal
  this commit, or a later one recorded here with the same kind of note.

### 2026-10-02: code commit re-pinned to `28fad2b0c26f762410ea80a2651f04a719673e6b` (before any real-data run)

- Commits since `c3e9f3d`:
  - the review gate (`orb backtest` refuses unreviewed manual rows);
  - `calendar_exceptions.csv` and `budget_days.csv` built from reviewed
    manual files;
  - a `budget_day` regime table (report only);
  - the user's review sign-offs.
- No change to signal, trade, sizing or cost logic. The newly excluded
  outage days are reference data and are covered by each run's data version.
- The config hash is unchanged:
  `0822eb04daa9cd48e788a8bd75152743a77a2cf6931e884dfd144e0af62eea0b`.

### 2026-10-02: code commit re-pinned to `5f1548e27f6a83b8a76298f9098949e1793bde2a` (before any real-data run)

- Since `28fad2b`, CSV I/O was centralised (explicit quoting, file names in
  read errors) and `orb dq` gained a ragged-row check. Budget days were
  confirmed. No change to signal, trade, sizing or cost logic, and the config
  hash is unchanged.

### 2026-10-02: code-provenance rule (replaces per-commit re-pinning; before any real-data run)

1. **Pin.** The code commit is pinned automatically by the **first** successful
   in-sample `orb backtest`. It's written to that run's `meta.json`
   (`provenance.pinned_commit`) and to `data/_runs/insample_pin.json`. The
   pin is never overwritten.
2. **Clean tree.** `orb backtest` refuses to run on a dirty working tree
   (tracked or untracked changes), in-sample and OOS alike.
3. **Logged changes.** After the pin, every commit touching result-relevant
   code must be logged in `docs/CHANGELOG_RESEARCH.md`, its hash (at least 7
   characters) included, with the reason and before/after metrics.
   Result-relevant code means signal, scoring, sizing, fills, costs and the
   engine: `src/orb/{signals,features,context,scan}.py`,
   `src/orb/scoring/`, `src/orb/sim/`,
   `src/orb/{portfolio,engine,invariants}.py`, `src/orb/data/adjust.py` and
   `config/`. `orb backtest` diffs the history since the pinned commit against
   these paths and refuses to run if a relevant commit isn't logged.
4. **OOS.** OOS needs the pin, records its own commit, and stores the diff
   summary from the pinned in-sample commit (commits, `diff --stat`, relevant
   files). `report.md` prints that summary.

The config hash
(`0822eb04daa9cd48e788a8bd75152743a77a2cf6931e884dfd144e0af62eea0b`) and the
success criteria a–e remain frozen as above.

### 2026-10-02: factor drift is a warning; two secondary diagnostics (before any real-data run)

- **Factor drift is a DQ warning, not an error.** Drift days are kept, and
  `factor_drift.csv` is copied into every run's report. Two checks remain
  errors (the stock-day is excluded):
  1. the four Kite/bhavcopy price ratios (O/H/L/C) on a day disagree by more
     than 0.5% (`deadjust_inconsistent`);
  2. the rebuilt 1-min high or low is more than 0.5% from NSE's official
     (bhavcopy) high or low (`daily_minute_mismatch`; it was a warning
     before).
- **Secondary diagnostics in `report.md`, excluded from pass/fail.** Each shows
  trades, rupee-rounded net at 1× and 2× slippage, and the null p-value, for
  the primary book:
  1. *Drift sensitivity:* trades on factor-drift stock-days removed.
  2. *Post-2020:* trades from 2020-01-01 onward (the survivorship gap is
     8–9% in 2017–2019).

  Trades are removed from the existing book; freed slots are not reallocated.
- No config value changed; the config hash is still
  `0822eb04daa9cd48e788a8bd75152743a77a2cf6931e884dfd144e0af62eea0b`.

