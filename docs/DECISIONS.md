# Design decisions (V1)

Resolutions of the spec ambiguities raised before implementation. Each is a fixed
research default; where a number is involved it lives in `config/`.

| # | Topic | Decision |
|---|-------|----------|
| 1 | Candle timestamps | Timestamp = candle **start** (Kite convention). 14:30 signal candle is inclusive, so the last possible entry is the 14:31 open. |
| 2 | Day-level filters | Stock-days failing the OR-width filter are still scanned for their first breakout; it is logged as `FILTERED_OR_WIDTH` with full features and labels. |
| 3 | Slots full | Stock is done for the day. Only the first breakout ever counts. |
| 4 | Entry gaps through stop | Skip as `INVALID_ENTRY_GAP` (risk ≤ 0). Label still computed where defined. |
| 5 | Missing candles | A missing minute means no stop check. A missing entry or 15:10 candle uses the next available open, then the last close, and is flagged. |
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
| 28 | Regimes | VIX: previous-day close terciles from the in-sample distribution. Trend day: Nifty 200 \|C−O\|/(H−L) ≥ 0.5. Expiry: index-weekly and stock-monthly tagged separately from `expiries.csv`. Results season: the stock's own results date ±3 sessions from `results_dates.csv`. |
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
| 39 | Excluded stock-days | Not scanned at all (DQ error, F&O ban, corporate action, special session or calendar exception). They are counted in the exclusion report instead. |
| 40 | Decision order | `INSUFFICIENT_HISTORY` (ATR or RV), then `MARKET_DATA_MISSING`, then `FILTERED_OR_WIDTH`, then `SCORE_BELOW_THRESHOLD`, then `QUALIFIED`. A filtered signal still gets a score, so its label can feed score-validity tables. |
| 41 | Index choice | Per day: Nifty 200 if it has no DQ error and has a 09:15 candle, otherwise Nifty 50 (`index_substituted = true`, logged). If neither is usable: `MARKET_DATA_MISSING`. r_idx uses the last index close at or before t. |
| 42 | Scorer interface | The scanner calls only `Scorer.score(features)`, with features `side` (+1 or −1), `d`, `rv`, `r_idx`, `w`, `v`. `explain()` is optional and used for logging only. Tested with a non-rule scorer. |
| 43 | Spin-off index treatment | Temporary inclusion of a demerged entity in Nifty 200, and its exclusion a few days later, are non-events for the universe. They are not tradable in a meaningful way, and they are often listed under a dummy symbol. |
