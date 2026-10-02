# Research changelog

Changes made after any real-data result (see `docs/PREREGISTRATION.md`). Only
bug fixes are allowed under V1; any spec change starts V1.1.

For each entry, record:

- **Date**
- **Commit: `<hash>`**: the commit being logged (at least 7 characters). `orb backtest` looks for this hash, and refuses to run if a result-relevant commit since the pin is missing
- **Reason**: the bug, and how it was found
- **Before / after** for every criterion a–e, from the primary book: trades,
  net at 1× and 2×, null p, positive-year share, and net R by score bucket
- **Runs**: the run ids that were compared

## 2026-10-02: report-only additions after the first in-sample run (V1 accepted as FAIL)

- **Commit: `0937f6509910bda85b0fc41400e1ae4b132c757d`**
- **Reason**: two bug checks requested after the first run.
  1. Max drawdown (₹22,437.54) is larger than the ₹10,000 capital, yet the
     "cash never negative" invariant passed. This is not a bug: it follows from
     the V1 capital model. The cash ledger in `invariants.py` starts **every
     day** at ₹10,000, and sizing (`sim/trade.py:size`) uses only the fixed
     slot size and the ₹100 risk cap, never cumulative P&L. So cash can't go
     negative within a day, while losses add up across days without ever
     shrinking a position. Sizing was not changed (V1 as specified).
     `report.md` now states the model and adds an account-ruin-date line: the
     first day cumulative rupee-rounded net P&L reaches −₹10,000. For this run,
     that is **2020-05-15**.
  2. Long/short split added as a regime table (`regime_side.csv`). The short
     P&L sign was checked by hand from the raw 1-min bars on 3 trades (MSUMI
     2022-12-20 stop; L&TFH 2019-07-08 hard-exit win; BIOCON 2023-09-22
     hard-exit loss). Entry, exit and gross match the engine to the paisa. No
     sign bug.
- **Before / after**: unchanged. Only `reports.py` changed; no
  result-relevant module did. The report was regenerated from the existing
  run, and the backtest was not re-run. The primary book is the same before
  and after:
  - trades: 5,370
  - net (rupee-rounded): −22,026.74 at 1× and −31,318.15 at 2×
  - null p: 0.353
  - positive years: 0/7
  - net R by score bucket: −0.144 / −0.117 / −0.092
- **Runs**: `20261002T213846_0822eb04` (report regenerated)
