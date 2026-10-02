# Data sources and reference-file formats

## Pipeline

```
orb ref all        NSE / niftyindices.com  ->  data/ref/*.csv  +  data/raw/daily (bhavcopy)
orb download       Kite (or vendor files)  ->  data/vendor/<provider>/snapshots/<id>/  (as delivered)
orb snapshot freeze                        ->  hashed, read-only, immutable
orb build-raw      frozen snapshot + bhav  ->  data/raw/minute  (actual traded prices) + raw/BUILD.json
orb dq             data/raw                ->  data/_dq/ (issues, exclusions, exclusion report)
```

Everything downstream (features, backtest) reads `data/raw/` only.

## Kite price adjustment (confirmed)

Kite historical candles are **already adjusted**. Zerodha staff on the Kite Connect forum:

- Prices are adjusted for bonuses, splits, rights issues, spin-offs and
  extraordinary dividends (>2%). Volumes are adjusted too, except for dividends.
- Adjustment is applied in the beginning-of-day process on the ex-date, and
  past candles **can change** when re-fetched later.
- Kite provides **no unadjusted series** and no list of the factors it applied.
  Data from before about 2018/19 may not be adjusted, and one user reported
  SBIN appearing unadjusted.

So our own `adjust.py` must never be applied on top of Kite data, and Kite's
adjustment can't be trusted to be complete. `orb build-raw` therefore
**de-adjusts** every stock-day against the official NSE bhavcopy, which is raw:
`ratio = bhavcopy / kite` (median over O, H, L, C). Raw minute bars are then
`kite × ratio` and `volume ÷ ratio`. Days where the four ratios disagree by more
than `data.deadjust_tolerance`, or that have no bhavcopy row, are DQ errors. This
handles any adjustment method, partial coverage, or none at all (ratio = 1).
`adjust.py` then works on raw data only, to put cross-day features (ATR, previous
close, RV baseline) into each trade date's own price terms.

Test: `tests/test_deadjust.py` uses **real** bhavcopy prices for the IRCTC 1:5
split (ex-date 2021-10-28) and the SRF 4:1 bonus (ex-date 2021-10-13). After
de-adjusting a Kite-like series, the raw store holds the prices that actually
traded. An integration variant runs against real downloaded data:
`ORB_DATA_ROOT=data uv run pytest -k integration`.

**Snapshot note:** because Kite can revise past candles, the as-delivered vendor
store is kept, and its content is part of the run's `data_version` hash.

## Source availability (checked 2026-10-02)

| Need | Source | Automated? |
|---|---|---|
| 1-min stock OHLCV | Kite historical (60-day windows, about 3 requests/s) | yes: `orb download` |
| 1-min NIFTY 200 / NIFTY 50 / INDIA VIX | Kite index tokens | yes. Verify how far back minute history goes |
| Raw daily OHLCV | NSE CM bhavcopy: legacy `cmDDMONYYYYbhav.csv.zip` up to 2024-07-05; UDiFF `BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip` from 2024-07-08 | yes: `orb ref bhavcopy` |
| F&O ban list | `nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_DDMMYYYY.csv` | yes: `orb ref ban` |
| Corporate actions | `www.nseindia.com/api/corporates-corporateActions` (JSON, by month) | yes: `orb ref ca` |
| Symbol renames | `nsearchives.nseindia.com/content/equities/symbolchange.csv` | yes: `orb ref symbols` |
| Mergers | not published as a structured file | **manual**: `manual/mergers.csv` |
| Current Nifty 200 | `niftyindices.com/IndexConstituent/ind_nifty200list.csv` | yes |
| Nifty 200 changes | niftyindices.com press releases (PDF) | yes, parsed. Unparsed ones go to a review list |
| Delisted / merged-away stocks, 1-min | not in Kite | **vendor** (TrueData, GDFL, Accelpix, ...) via the local provider |
| Expiry calendar | NSE F&O bhavcopy (legacy `foDDMONYYYYbhav.csv.zip` / UDiFF `BhavCopy_NSE_FO_…`), one trading day sampled per week | yes: `orb ref expiries` |
| Results dates | `www.nseindia.com/api/corporate-board-meetings` (JSON, by month); purpose or description mentions financial results | yes: `orb ref results` |

All downloads are rate-limited (`data.nse.max_requests_per_sec`) and cached
under `data/ref/_cache/<source>/`, including 404 markers for holidays, so
reruns are resumable and work offline.

## Manual downloads

Put these files under `data/ref/manual/`:

1. **`mergers.csv`**, with columns `old_symbol,new_symbol,effective_date[,needs_review,...]`.
   `orb ref mergers` pre-fills candidates with `needs_review = true`, and only
   rows set to `false` are applied. These
   are mergers and amalgamations where the old symbol stops trading: for example
   `HDFC,HDFCBANK,2023-07-13`. Sources: the NSE corporate-actions entries marked
   "Scheme of Amalgamation", and niftyindices "Replacement in indices … on account
   of scheme of amalgamation" press releases. `orb ref symbols` merges this file
   into `symbol_map.csv` with `change_type = merger`.
2. **`nifty200_changes_manual.csv`**, with columns
   `effective_date,symbol,change,source`, where `change` is `add`, `remove` or
   `ignore`. Fill it from `data/ref/nifty200_review.csv`, which lists every press
   release that mentions Nifty 200 but couldn't be parsed. Open each PDF URL in
   that file and record the Nifty 200 changes. Manual rows replace parsed rows
   for the same `(effective_date, symbol)`; `ignore` deletes a parsed row. Then
   rerun `orb ref nifty200`. Also check `data/ref/nifty200_sizes.csv`: any date
   where the index doesn't hold exactly 200 names points to a missed change.
   Spin-offs are deliberately ignored: Nifty adds the demerged entity
   temporarily, often under a dummy symbol before it lists, and excludes it
   days later (JIOFIN 2023, ITC Hotels 2025, TML CV 2025, the Vedanta entities
   2026). Both steps are treated as non-events.

   **Status (2026-10-02).** 602 releases since 2017-10 were parsed. Text
   extraction handles wrapped table rows and "NIFTY 200 Index" headings, and
   image-only PDFs are OCR'd for review (one such release: 2021-08-23). A
   drafted `nifty200_changes_manual.csv` (29 rows, each citing a PDF and page)
   covers:
   - the 2020-03-27 review, declared null and void because of COVID;
   - the OCR'd 2021-09-30 review;
   - the IREDA→BSE revocation (2024-03-28);
   - the TATAMTRDVR exclusion (2024-08-30);
   - the IDEA/CENTRALBK revocations (2024-09-30).

   With it applied, **every trading day (2,220 days, 2017-10-03 to
   2026-09-30) holds exactly 200 companies**; `orb ref nifty200` writes the daily
   count to `nifty200_daily_counts.csv` and warns on any other day. The 201 *securities* during the TATAMTRDVR periods are
   expected. Rows marked `needs_review = true` (the OCR transcriptions) still
   need checking against the PDF.
3. **Delisted and merged-away stocks.** `orb download` lists them as
   `delisted:` / `merged:`. Buy their 1-min history from a vendor, put one file
   per symbol in `vendor/minute/<SYMBOL>.csv` (or `.parquet`), and import with
   `data.provider: local`.

## Reference files (`data/ref/`, CSV or Parquet, dates `YYYY-MM-DD`)

| File | Required | Columns | Notes |
|---|---|---|---|
| `nifty200_membership.csv` | yes | `date,symbol` **or** `symbol,valid_from,valid_to` | Built by `orb ref nifty200` (interval form). `valid_to` is inclusive; empty means still a member. Overlapping intervals are rejected. Symbols are the names in use on each date. |
| `fo_ban.csv` | yes | `date,symbol` | |
| `corporate_actions.csv` | yes | `symbol,ex_date,action_type,price_factor` (+ `subject`, `face_value`, `rights_*`) | `action_type` ∈ split, bonus, rights, demerger, merger, dividend, other. `price_factor` multiplies pre-ex-date prices: a 1:5 split gives 0.2, bonus a:b gives b/(a+b). It's required for split and bonus. For rights it is filled from TERP (theoretical ex-rights price) using the raw close before the ex-date. For demergers it is left empty; the ex-date is still excluded, and DQ flags any unadjusted jump. |
| `symbol_map.csv` | no | `old_symbol,new_symbol,effective_date[,change_type]` | `change_type` ∈ `rename` (default when the column is absent) or `merger`. `new_symbol` is the symbol in use **from** `effective_date`. Chains are followed (LTI → LTIM → LTM). Renames are fetched under the latest symbol and stored under the historical one. A merger is never followed: the acquirer's prices are not the target's. |
| `special_sessions.csv` | yes | `date,session_type` | muhurat, mock, dr, special, other. These days are excluded. |
| `calendar_exceptions.csv` | no | `date,reason` | Halts and abnormal sessions; excluded. |
| `budget_days.csv` | no | `date` | Tagged, not excluded. |
| `expiries.csv` | no | `date,expiry_type,underlyings` | `index_weekly`, `index_monthly`, `stock_monthly`. Per underlying and calendar month, the last expiry is monthly and the others are weekly; stock expiries are always monthly. |
| `results_dates.csv` | no | `symbol,date` | `date` = board-meeting date. The `results_day` tag is that date (if it's a trading day) plus the next trading day. |

## Exchange

The backtest assumes **NSE** fills and NSE charges (`execution.exchange: NSE`,
`costs.yaml: exchange: NSE`). If live order code is ever written, it must set
`exchange=NSE` explicitly on every order rather than relying on the broker's
default routing, or the cost and tick assumptions here won't hold.
