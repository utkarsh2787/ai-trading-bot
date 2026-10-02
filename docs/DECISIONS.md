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
| 8 | RV history | All 20 prior sessions required; missing minutes count as zero volume. |
| 9 | Price basis | Intraday OR/signal/fills use **raw** prices (tick + costs depend on the real price). Cross-day features (ATR, prev close, 20-day volume) use corporate-action-**adjusted** series computed by `orb.data.adjust`. |
| 10 | Index fallback | Decided **per day**: if Nifty 200 1-min is unavailable/fails DQ for a day, Nifty 50 is used and logged. |
| 11 | Hard filters | Live in the engine, not the scorer, so any future scorer is judged on the same candidates. |
| 12 | Factor breakdown | `Scorer.score()` returns a float; optional `explain()` returns components for logging only. |
| 13 | Membership file | Daily snapshot `(date, symbol)` or change log `(symbol, valid_from, valid_to)`; normalised to intervals. |
| 14 | Corporate-action exclusion | Only price-adjusting actions (split, bonus, rights, demerger, merger) exclude the stock-day. Dividends are tagged, not excluded. |
| 15 | Symbol changes | `symbol_map.csv (old_symbol, new_symbol, effective_date)`, supplied as reference data. |
| 16 | Special sessions | Muhurat, DR/mock sessions excluded via `special_sessions.csv`. Budget day traded and tagged `is_budget_day`. |
| 17 | Max positions | **3 concurrent**. A slot freed by an exit during candle T can be used by an entry at the open of T+1 or later. |
| 18 | Ties | Same-minute signals: score descending, then symbol ascending. |
| 19 | Capital | ₹10,000 fixed every day; no compounding. |
| 20 | Tick rounding | Slippage-adjusted fills are rounded adversely to the tick grid (buys up, sells down). |
| 21 | Tick table | Date-effective table in `config/tick_sizes.yaml`; band chosen by previous close. **Marked unverified**; confirm against the NSE circulars. |
| 22 | Slippage stress | 2×/3× scale both the tick and percentage parts. |
| 23 | Costs | Date-effective table in `config/costs.yaml`, seeded with current Zerodha/NSE rates for all dates. **Marked unverified.** No rupee rounding. |
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
