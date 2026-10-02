# Design decisions (V1)

Resolutions of the spec ambiguities raised before implementation. Each is a fixed
research default; where a number is involved it lives in `config/`.

| # | Topic | Decision |
|---|-------|----------|
| 1 | Candle timestamps | Timestamp = candle **start** (Kite convention). 14:30 signal candle is inclusive, so the last possible entry is the 14:31 open. |
| 2 | Day-level filters | Stock-days failing the OR-width filter are still scanned for their first breakout; it is logged as `FILTERED_OR_WIDTH` with full features and labels. |
| 3 | Slots full | Stock is done for the day. Only the first breakout ever counts. |
| 4 | Entry gaps through stop | Skip as `INVALID_ENTRY_GAP` (risk ≤ 0). Label still computed where defined. |
| 5 | Missing candles | A missing minute means no stop check. A missing entry candle uses the next available open, but no later than 14:31 (else `NO_ENTRY_DATA`). A missing 15:10 candle exits at the last close before 15:10 (`EXIT_FALLBACK`), so no position is ever held past 15:10. |
| 6 | Halts / abnormal sessions | Excluded via `calendar_exceptions.csv`; DQ flags abnormal session timing. |
| 7 | Wilder ATR seed | 14-bar SMA seed from the fixed `data.daily_history_start`; ATR is only used after `features.min_daily_bars` (100) daily bars. |
| 8 | RV history | *Superseded by 32.* Missing minutes count as zero volume. |
| 9 | Price basis | Intraday OR/signal/fills use **raw** prices (tick + costs depend on the real price). Cross-day features (ATR, prev close, 20-day volume) use corporate-action-**adjusted** series computed by `orb.data.adjust` **from raw data only** (see 30), rebased to the trade date's own price terms. |
| 10 | Index fallback | Decided **per day**: if Nifty 200 1-min is unavailable/fails DQ for a day, Nifty 50 is used and logged. |
| 11 | Hard filters | Live in the engine, not the scorer, so any future scorer is judged on the same candidates. |
| 12 | Factor breakdown | `Scorer.score()` returns a float; optional `explain()` returns components for logging only. |
| 13 | Membership file | Daily snapshot `(date, symbol)` or change log `(symbol, valid_from, valid_to)`; normalised to intervals. |
| 14 | Corporate-action exclusion | Only price-adjusting actions (split, bonus, rights, demerger, merger) exclude the stock-day. Dividends are tagged, not excluded. Split/bonus factors are parsed from the NSE subject; rights factors come from TERP; demerger factors are left empty (see 37). |
| 15 | Symbol changes | `symbol_map.csv (old_symbol, new_symbol, effective_date, change_type)`. Renames come from NSE `symbolchange.csv`; mergers from a manual file. Unknown provider symbols are classified as renamed (fetched under the latest name, stored under the historical one), merged (never mapped to the acquirer's prices) or delisted. |
| 16 | Special sessions | Muhurat, DR/mock sessions excluded via `special_sessions.csv`. Budget day traded and tagged `is_budget_day`. |
| 17 | Max positions | **3 concurrent**. A slot freed by an exit during candle T can be used by an entry at the open of T+1 or later. |
| 18 | Ties | Same-minute signals: score descending, then symbol ascending. |
| 19 | Capital | ₹10,000 fixed every day; no compounding. |
| 20 | Tick rounding | Slippage-adjusted fills are rounded adversely to the tick grid (buys up, sells down). |
| 21 | Tick table | **Verified** by the user. Flat 0.05 before 2024-06-10; two bands from 2024-06-10; six bands from 2025-04-15. The band is set by the raw close on the last trading day of the previous month and holds for the whole month. If the stock didn't trade in the previous month, the last earlier close is used; with no earlier close there is no tick, so no trade. |
| 22 | Slippage stress | 2×/3× scale both the tick and percentage parts. |
| 23 | Costs | Date-effective table. SEBI fee and the GST base are verified against a contract note; the NSE exchange-txn rate (0.00297%), brokerage, STT and stamp duty are not (the loader warns). STT and stamp duty are rounded to the rupee per contract note (per day), and the rounded total is allocated back to orders pro rata. Reports show results with and without rounding. NSE only. |
| 24 | Labels | Per-share labels (R, MFE/MAE in R and %, exit reason) always; rupee labels when qty ≥ 1 and score defined. |
| 25 | R / excursions | Gross and net R. MFE/MAE from the entry candle through the exit candle inclusive. |
| 26 | Entry-candle variants | Labels computed for both the default and conservative variants. |
| 27 | Dates / OOS | Trades from `2018-01-01`; fixed `oos_start = 2024-10-01`. OOS requires `--oos`, is recorded in a ledger, and a rerun is refused unless forced with a logged reason. |
| 28 | Regimes | VIX: *superseded by 54.* Trend day: Nifty 200 \|C−O\|/(H−L) ≥ 0.5. Expiry: index-weekly and stock-monthly tagged separately from `expiries.csv`. Results season: the stock's own results date ±3 sessions from `results_dates.csv`. |
| 29 | Null test | Random direction at the same entry times over taken trades, stop mirrored at the same per-share risk, same hard exit. B = 2000, fixed seed. Paired bootstrap CI of the mean per-trade net P&L difference + fraction of random runs beating actual. |

## Library choice

**polars** for storage, data-quality checks and features: lazy hive-partitioned Parquet scans,
fast group-bys/as-of joins, strict dtypes and no implicit index (fewer silent
alignment bugs, which matters for look-ahead safety). Per-trade path simulation
runs on numpy arrays extracted from polars. **pydantic v2** validates config.

## Additions (review round 2)

| # | Topic | Decision |
|---|-------|----------|
| 30 | Kite adjustment | Kite candles are already adjusted, for both prices and volume (confirmed by Zerodha staff). There is no raw series and past candles can be revised. Raw minute bars are rebuilt per stock-day as `kite × (bhavcopy / kite)`, using the median O/H/L/C ratio; days where the ratios disagree by more than 0.5%, or with no bhavcopy row, are DQ errors. `adjust.py` is applied to raw data only, so nothing is adjusted twice. |
| 31 | DQ bias | Volume spikes: warn only. Overnight gaps, measured on the adjusted series: >10% is a warn (`news_gap`, kept); >25% with no price-factor action is an error (`extreme_gap`); >25% on a known split/bonus ex-date is an error (`adjustment_mismatch`, meaning the price looks unadjusted relative to the known ratio). |
| 32 | RV baseline | Mean of the most recent `rv_lookback` (20) **valid** sessions within the last `rv_window_trading_days` (30) market trading days. A session is valid if the stock-day isn't excluded (DQ error, ban, corporate action, special session) and has minute data. Fewer than `rv_min_valid_sessions` (15) means `INSUFFICIENT_HISTORY`. With 15–19 valid sessions, the mean is over those available. |
| 33 | ATR validity | No signal (`INSUFFICIENT_HISTORY`) if any of the last `atr_period` (14) market trading days before T lacks a valid daily bar for the stock, or fewer than `min_daily_bars` (100) bars exist. |
| 34 | Index days | Index instruments never get volume checks (zero or missing volume is normal); only price and timestamp checks apply. |
| 35 | Exclusion report | Excluded point-in-time member stock-days by reason, year and India VIX tercile (previous-day close, in-sample cut points), with % of universe stock-days. |
| 36 | Nifty 200 history | Rebuilt backwards from the current constituents using parsed niftyindices press-release PDFs, symbol renames and manual corrections. Every date is checked to hold exactly 200 names. |
| 37 | Rights / demerger factors | Rights: TERP / cum price, using the raw close before the ex-date and face value + premium as the issue price. Demerger: no factor (it needs post-listing values); the ex-date is excluded and the DQ gap rule flags any jump. |
| 38 | Breakout scan window | Only candles 09:30–14:30 are scanned. A first breakout after 14:30 couldn't be traded, so it isn't logged. |
| 39 | Excluded stock-days | *Superseded by 46.* |
| 40 | Decision order | `INSUFFICIENT_HISTORY` (ATR or RV), then `MARKET_DATA_MISSING`, then `FILTERED_OR_WIDTH`, then `SCORE_BELOW_THRESHOLD`, then `QUALIFIED`. A filtered signal still gets a score, so its label can feed score-validity tables. |
| 41 | Index choice | Per day: Nifty 200 if it has no DQ error and has a 09:15 candle, otherwise Nifty 50 (`index_substituted = true`, logged). If neither is usable: `MARKET_DATA_MISSING`. r_idx uses the last index close at or before t. |
| 42 | Scorer interface | The scanner calls only `Scorer.score(features)`, with features `side` (+1 or −1), `d`, `rv`, `r_idx`, `w`, `v`. `explain()` is optional and used for logging only. Tested with a non-rule scorer. |
| 43 | Spin-off index treatment | Temporary inclusion of a demerged entity in Nifty 200, and its exclusion a few days later, are non-events for the universe. They are not tradable in a meaningful way, and they are often listed under a dummy symbol. |

## Additions (review round 3)

| # | Topic | Decision |
|---|-------|----------|
| 44 | Factor drift | The daily Kite/bhavcopy factor (median of the O/H/L/C ratios) should be constant within 0.1% (`data.deadjust_drift_tolerance`) between consecutive corporate-action ex-dates of any type. The reference is the segment median. Drifting days are DQ warnings (`deadjust_factor_drift`; see 64), and `data/_dq/factor_drift.csv` lists each run: symbol, start, end, days, segment factor, min, max. A drift also shows up when Kite adjusted for an action missing from our corporate-action file. |
| 45 | Vendor snapshots | `orb download` writes into `vendor/<provider>/snapshots/<id>/`. `orb snapshot freeze` hashes every file and makes it read-only. `build-raw` only reads a frozen, verified snapshot and records its id and content hash in `raw/BUILD.json`. The run data version = hash(snapshot hash, raw store, reference files). |
| 46 | Excluded stock-days | Scanned and logged with every computable feature, with decision `EXCLUDED` and reason `EXCLUDED_SESSION`, `EXCLUDED_DQ`, `EXCLUDED_CORP_ACTION` or `EXCLUDED_BAN` (precedence in that order; all raw reasons are kept in `exclusion_detail`). They are never traded, and never used as RV baseline sessions. |
| 47 | Survivorship gap | % of point-in-time member stock-days on trading days with no 1-min data, by year (`_dq/survivorship_gap.csv`, and per run). It is printed as the first line of every backtest summary. Nothing blocks on vendor data. |
| 48 | Nifty 200 size check | Counts companies: Tata Motors DVR is a second share class (the index held 201 securities while TATAMTRDVR was a member). |
| 49 | Manual index changes | `manual/nifty200_changes_manual.csv` (`effective_date, symbol, change, source, needs_review, note`) is applied on every build. Rows transcribed by OCR or inferred are `needs_review = true`. Image-only PDFs are OCR'd (poppler + tesseract) for review, never parsed automatically. |
| 50 | Merger candidates | `orb ref mergers` pre-fills `manual/mergers.csv` from press-release "amalgamation of X with/into Y" text, mapped to symbols via release tables and confirmed by the target's bhavcopy history ending. All rows are `needs_review = true`. `orb ref symbols` applies only reviewed rows. Share counts aren't in public NSE data, so the share-count-jump signal isn't used. |
| 51 | Engine | Every signal is simulated for each variant × slippage multiplier, sized as if taken. The portfolio then books per run. In the conservative variant, a stop on the entry candle fills at the stop minus slippage (the open is the entry). Contract-note rounding is computed per day over that run's taken orders. Results are reported with and without it. |
| 52 | Engine invariants | Checked on every (day, variant, slippage) book; any violation fails the run: ≤ 3 open positions and ≤ ₹8,000 deployed at every slot; nothing open after 15:10; no signal after 14:30 and no entry after 14:31; one trade per stock per day; cash never negative (a ledger with entry charges paid at entry); per-trade risk ≤ ₹100; sum of trade net P&L = day equity change to ₹0.01, with and without rounding. |
| 53 | Regime reference files | Expiries come from weekly samples of the F&O bhavcopy: per underlying and month, the last expiry is monthly and the rest are weekly (index); stock expiries are monthly. Results dates are NSE board meetings whose purpose or description mentions financial results; `results_day` = the meeting date or the next trading day (this supersedes the ±3-session window in 28). |
| 54 | VIX terciles | Source: Kite `INDIA VIX` index daily bars (snapshot → `raw/daily`; NSE's VIX history API returned 503). On day d the previous day's close is tagged, against tercile cut points from every close up to and including d−1 (expanding window, past data only). At least 250 prior sessions are required, else untagged. |
| 55 | Null test | Primary (gates criterion c): for each taken trade in the primary book, a twin with only the direction flipped. It has the same entry candle, the same qty, the same rupee costs, and a stop mirrored at the same per-share distance from its entry fill; it exits by the same rules (that stop, else 15:10). The exit time therefore matches the actual trade unless one of the two stops is hit; `same_exit_time_share` reports how often it does. Secondary (reported only): a strict sign flip at the actual exit time, −gross − costs. Each of 2,000 draws (seed `run.seed`) picks every trade's side with a fair coin. Reported: one-sided p = P(random total ≥ actual) and a paired-bootstrap 95% CI of the mean per-trade difference. |
| 56 | Score validity | Uses the labels (default variant) of every scored first breakout, including rejected and filtered ones; EXCLUDED stock-days are left out. Buckets: <65, 65–74, 75–84, 85+. Quintiles are of the raw factors d, RV, r_idx × side, w, v. |
| 57 | Regime slices | Taken trades in the primary book, by VIX tercile (54), trend vs range day (Nifty 200 \|C−O\|/(H−L) ≥ 0.5, a same-day ex-post tag used for reporting only), each expiry type and any expiry, and stock results day (53). |
| 58 | Merger dates | Universe membership follows the **Nifty 200** exit date only. That date comes from the rebuilt membership (the Nifty 200 sections of the index press releases); merger rows never change membership. `mergers.csv` `effective_date` (the day after the target's last trade) ends the price history and the symbol mapping only. A merger release's quoted date is a cross-check that may concern other indices (`release_applies_to_nifty200`). A stock removed from the index is out of the universe from the removal date even while it still trades. |
| 59 | Special sessions | Draft built by `orb ref sessions` into `manual/special_sessions_draft.csv`. Bhavcopy signals: a weekend session; market turnover < 30% of the ±10-session median. 1-min signals: median first candle > 09:45; last candle < 15:00 or after 15:29; median candles < 300. Each row has `detection_reason` and `proposed_action` (`exclude`, or `tag_budget` for full-length Saturday budget sessions), and starts at `needs_review=true`. Only reviewed `exclude` rows reach `special_sessions.csv`. Bars on excluded whole-market days are also dropped from the daily ATR history. The bhavcopy downloader now tries weekends as well. |
| 60 | Outages and budget days | Outage days go in `manual/calendar_exceptions_manual.csv` and are excluded market-wide; their daily bars also leave the ATR history: 2017-07-10 (confirmed), and 2021-02-24 (pending: verify against 1-min data). Budget days go in `manual/budget_days_manual.csv`; they're traded and tagged, with a `budget_day` regime table: 2020-02-01 and 2025-02-01 (full-length Saturday sessions). `orb ref sessions` builds `calendar_exceptions.csv` and `budget_days.csv` from reviewed rows only. |
| 61 | Review gate | `orb backtest` refuses to run while any row of any `manual/*.csv` with a `needs_review` column is not `false`. |
| 62 | CSV I/O | All CSV reads and writes go through `orb.csvio`. Writes use explicit RFC-4180 quoting (any field with a comma, quote or newline is quoted) and atomic replace. Read errors name the file, or the download URL for in-memory files. `orb dq` first scans every CSV under `data/ref` (except `_cache`) for ragged rows, writes `_dq/ref_csv_issues.csv`, and stops with the file:line of each one. |
| 63 | Code provenance | The first in-sample run pins the code commit. `orb backtest` refuses a dirty tree, and refuses any result-relevant commit since the pin that isn't logged by hash in `CHANGELOG_RESEARCH.md`. Runs after the pin (OOS in particular) record and report the diff since the pin. See the PREREGISTRATION amendment "code-provenance rule". |
| 64 | Drift = warning | Drift days are kept. On drift days the rebuilt 1-min high/low is within 0.5% of NSE's official high/low on 98.6% of days (clean days: 98.8%); the median error is ~10 bp, a uniform shift for the day. Errors remain when the O/H/L/C ratios disagree by more than 0.5%, or the rebuilt 1-min high/low is more than 0.5% off the bhavcopy. Secondary diagnostics in `report.md`: drift-excluded and post-2020 headline metrics (not pass/fail). |
| 65 | Volume validation | Kite 1-min volume is unadjusted before some splits and bonuses. In `build-raw`, on a day before the symbol's split/bonus, if sum(1-min volume) / bhavcopy quantity is within 5% of the cumulative later factor F, that day's 1-min volume is divided by F. Corrected days go in `_dq/volume_corrections.csv`. `orb dq` then flags `volume_mismatch` (an error) when the post-fix ratio is outside [0.80, 1.02]; it does not apply to indices. See the PREREGISTRATION amendment of 2026-10-02. |
