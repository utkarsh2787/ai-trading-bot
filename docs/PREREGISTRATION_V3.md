# Pre-registration: V3, weekly short-term reversal (long-only, delivery)

Frozen on 2026-10-03, **before any V3 run** on any data. V3 results are judged
only against what is written here. V1 and V2 are closed as FAIL (see
`docs/PREREGISTRATION.md`, `docs/PREREGISTRATION_V2.md`); their records don't
change.

## Identity

| | |
|---|---|
| Spec version | **V3** (the V3 spec of 2026-10-02/03, with the gap resolutions below) |
| Strategies tested so far | **3** (V1, V2, V3). Bonferroni: criterion c uses p < 0.05 / 3 = **0.0167** (`null_p_max` = 0.0166…; with 2,000 draws, p moves in steps of 0.0005, so the two are identical) |
| Code commit | **pinned automatically by the first V3 in-sample run**, in its `meta.json` and in `data/_runs/v3/insample_pin.json` |
| Config hash | `10539881021f7f26f8b25568ed71930f26e21984bfe2310ff01014d2caed5a14` (`config/v3.yaml` + the tick table + the CNC cost table `config/costs_cnc.yaml`, including the `prereg` thresholds) |
| In-sample period | 2018-01-01 to 2024-09-30 |
| OOS start | 2024-10-01 (never run so far, by any strategy) |
| Data | The frozen Kite snapshot `20261002T143725`, the raw store and reference files, the DQ exclusions in `data/_dq`, and `data/ref/fno_stocks.csv`. No new downloads. Each run records its data version. |

## Hypothesis

Short-term reversal: the Nifty 200 stocks with the worst past-week return
outperform over the following week. Long-only, delivery (CNC).

**Note.** The strategy is long-only in a market that rose over the in-sample
period, so criterion b can pass on market beta alone. Criteria c and d,
against random selection from the same universe, are the real test.

## Universe and exclusions

- Point-in-time Nifty 200.
- The same DQ exclusions as V1/V2 (DQ errors, `volume_mismatch` included),
  applied on the signal day and on the exit day.
- Special sessions and calendar exceptions are never trading days.
- **The F&O ban does not exclude:** it restricts derivatives, not delivery.
- Corporate-action ex-days (split, bonus, rights, demerger, merger) exclude a
  stock from ranking and new buys on that day (gap #2). They never force a held
  stock out; see "Corporate actions during a hold".
- Warm-up: at least 6 prior daily bars, and a close on the previous rebalance
  day (gap #4). Stocks that newly joined the index qualify as soon as they have
  that history.

## Schedule

- **Rebalance day:** the last trading day of each calendar week, normally
  Friday. If Friday is a holiday or an excluded whole-market session, it is the
  previous trading day.
- Weeks with no trading day have no rebalance.

## Signal (causal, at 15:00 on the rebalance day)

| | |
|---|---|
| `P_now` | close of the 14:59 candle (closes at 15:00:00). If it is missing: the last close from 14:55 to 14:58. If there is none: `NO_PRICE`, not ranked |
| `P_prev` | official NSE close on the previous rebalance day × F(prev) / F(today), i.e. the product of split/bonus price factors with ex-date in (prev, today] (gap #5). No bar on that day → `NO_PREV_PRICE`, not ranked |
| `r_week` | `P_now / P_prev − 1` |
| rank | eligible stocks by `r_week` ascending (most negative first); ties by symbol, A→Z |
| target set | the 2 lowest-ranked stocks |

## Rebalancing (at the 15:00 candle open, plus slippage)

- **Held and still in the target set:** kept. No trade, no resize.
- **Held and not in the target set:** sold.
- **In the target set and not held:** bought, in rank order (rank 1 first),
  into the slots freed this week.
- **Order of the day's trades:** sells first, then buys (gap #7).
- **Execution candle:** the 15:00 open. If it is missing, the open of the
  first candle from 15:01 to 15:05.
  - A buy with no such candle: `ENTRY_MISSING`, and the slot stays empty that
    week.
  - A sell with no such candle: the official (bhavcopy) close, flagged
    `EXIT_NO_MINUTE_DATA` (gap #6).
- **Circuit guard** (as in V2: only stocks not in F&O on that date; a candle
  is locked when high == low == the day's extreme in the blocking direction).
  - **Entry locked:** the buy is skipped and the slot stays empty that week,
    flagged `LOCKED_CIRCUIT`.
  - **Exit locked:** search forward to the open of the first unlocked candle
    from 15:01 to 15:29 (`EXIT_LOCKED`). If there is none, the position is
    carried to the next trading day: the 09:20 open, or the first candle after
    09:20, with the guard applied again and a forward search to 15:29. If the
    stock is still locked all day, it carries again (`EXIT_CARRIED`, gap #9).
    The carry day's DQ exclusion doesn't block the exit.
- **A held stock that is DQ-excluded on its sell day** (gap #3): the sale is
  deferred to the next trading day on which that stock is not DQ-excluded, at
  its 15:00 open, flagged `EXIT_DEFERRED`.
- **Delisting or merger during a hold:** the position is closed at the last
  traded official close, with the cash credited that day (`EXIT_DELISTED`,
  gap #17). Mergers come from `mergers.csv`; a delisting is the end of the
  stock's daily history before the end of the data.
- **Positions open at the in-sample end** are sold at the last rebalance on or
  before 2024-09-30, paying all costs (`FORCED_END`, gap #21). They count as
  completed round trips.

## Sizing (tracks equity, cash only)

| | |
|---|---|
| Starting capital | ₹10,000 |
| `equity_w` | cash + the market value of holdings at the rebalance day's 14:59 close. If that is missing: the last close from 14:55 to 14:58, else the previous official close, flagged (gap #10). An excluded held stock is still valued at its 14:59 close; this value is used only for sizing |
| `Slot_w` | 0.90 × `equity_w` / 2; the 10% buffer covers charges |
| New buys | `qty = floor(Slot_w / entry fill)`. `qty < 1` → skip, `QTY_ZERO`. If the buy plus its charges (with ₹1 kept back for the day's STT and stamp rounding) would take cash below 0, qty is reduced until it fits, flagged `CASH_LIMITED` (gap #8) |
| Equity basis | rupee-rounded costs (contract note). Unrounded figures are reported on the same trades |
| Ruin stop | no new buys once `equity_w < ₹5,000`. The ruin date is the first such rebalance day. Held positions are still sold under the normal rules (gap #11) |
| Leverage | none; cash may never go negative (invariant) |
| Settlement | sell proceeds can pay for buys on the same day (**known limit**) |

Every slippage book has its own ledger and equity path.

## Corporate actions during a hold

- **Splits and bonuses:** `qty = floor(qty / price_factor)`. The fractional
  share is dropped and counted; in reality it would be paid in cash (gap #12).
  The holding's value is otherwise unchanged.
- **Cash dividends:** `qty × amount per share` is credited on the ex-date.
  - The amount is parsed from the `subject` of `corporate_actions.csv`: "Rs/Re
    X Per Share", or a bare "X Per Share".
  - Rows without an amount credit ₹0 and are counted (gap #13).
  - A dividend and a split on the same ex-date: the dividend is credited on
    the qty before the split.
  - The cash counts toward that week's `equity_w`. It is reported as its own
    line and included in that trade's net P&L (gap #14).
- **Taxes are ignored:** results are pre-tax, as stated in the report.
- **Demergers** (no price factor exists): the position is sold at the 15:00
  open on the trading day before the ex-date (ex-dates are announced in
  advance), flagged `EXIT_CORP_ACTION` (gap #15).
- **Rights issues:** never taken up. The position is held through with no
  adjustment, and rights ex-dates held through are counted (gap #16).

## Slippage

| Book | Per fill, against the trade | Role |
|---|---|---|
| `primary` | 1 tick, from the date-keyed monthly tick table; no rounding | **primary book**; criteria a–e |
| `primary_2x` | 2 ticks, no rounding | reported only |
| `v1_model` | 1 tick + 0.02% of price, rounded against the trade to the tick | **stress book**; criterion b |

## Costs (Zerodha CNC; `config/costs_cnc.yaml`; every field unverified except the DP charge)

| Charge | Rate |
|---|---|
| Brokerage | ₹0 |
| STT | 0.1% on the buy **and** on the sell |
| Stamp duty | 0.015% on the buy |
| Exchange txn | 0.00297%, the NSE equity rate used by V1. One flat rate for the whole period (**known limit**, gap #18) |
| SEBI fee | ₹10 per crore |
| GST | 18% on (exchange txn + SEBI fee) |
| DP charge | ₹15.34 per stock per sell day (₹3.50 CDSL + ₹9.50 Zerodha + ₹2.34 GST), verified against zerodha.com/charges (current rate). Constant over 2018–2024 (**known limit**; amended 2026-10-03, see Amendments) |

STT and stamp duty are rounded to the rupee per day (contract note), as in
V1/V2. Results are reported with and without rounding.

## Logging

- **Every rebalance day:** every member stock with `P_now`, `P_prev`,
  `r_week`, rank, target flag, decision and reason.
- **Every completed round trip:**
  - entry and exit date, time and fill; qty at entry and at exit;
  - gross; each cost line on each leg; DP; slippage; dividends;
  - net, rounded and unrounded;
  - exit reason, flags, holding days (trading days).
- **Labels:** for every eligible stock each week, the next-week return from
  this rebalance's 15:00 open to the next rebalance's 15:00 open (gap #19).
  - **Gross:** split/bonus adjusted, dividends included, no costs.
  - **Net:** primary slippage on both fills and one CNC round trip (DP
    included) at that week's primary-book `Slot_w`.

## Nulls

**Primary null (criterion c): random selection** (gap #20).
- Each week, 2 stocks are drawn uniformly at random from that week's eligible
  universe. A held stock that is drawn again is kept.
- Everything else matches the actual book: timing, sizing, guard, corporate
  actions, dividends, primary slippage, CNC costs with rupee rounding, the ruin
  stop and `FORCED_END`. Each draw runs its own equity path.
- 2,000 draws, seed 20180101.
- Statistic: **final equity**, dividends included. One-sided p = P(null final
  equity ≥ actual final equity). Passes at p < 0.0167.

**Secondary (reported, not pass/fail):**
- **Nifty 200 buy-and-hold** from the in-sample start, with no costs. This is
  the price index, so it excludes dividends.
- **Equal-weight universe, rebalanced weekly.** Every eligible stock at equal
  weight, rebalanced each week at the 15:00 open, using fractional shares: with
  ₹10,000 and about 190 stocks, whole shares are impossible. Two versions are
  reported:
  - percentage CNC costs on the turnover (STT, stamp, exchange, SEBI, GST);
  - plus the DP charge for each stock with a net sale that week.

  At this capital the DP charge dominates, which is why both are shown.

## Pre-registered success criteria (in-sample 2018-01-01 to 2024-09-30)

| | Criterion | Measured on |
|---|---|---|
| a | ≥ 300 completed round trips | primary book |
| b | net P&L (rupee-rounded) > 0 under primary **and** ≥ 0 under the V1-model stress | `primary` and `v1_model` books, each with its own equity path |
| c | beats the random-selection null at **p < 0.0167** (one-sided; final equity) | primary book |
| d | the year's net beats the random-selection null's mean annual net in ≥ 60% of calendar years (2018–2024; partial 2024 to 30 September counted) **and** the ruin stop is never hit | primary book. Annual net = equity marked to market at the official close on the year's last trading day, minus the same at the previous year end (₹10,000 at the start), computed the same way for every null draw |
| e | *informational:* the average next-week **gross** label return rises from the top `r_week` quintile to the bottom one (strictly). The net label version is also reported | labels of every eligible stock; quintiles of `r_week` within each week, pooled |

**Gate:** a–d must all pass in-sample. Criterion e doesn't gate.

`orb report --strategy v3` prints one `[PASS]`/`[FAIL]` line per criterion and
the gate verdict at the top of `report.md`. Then come the metrics per book, the
nulls, the secondary diagnostics and the flag counts.

## Out-of-sample

- OOS (2024-10-01 onward) is allowed only if a–d pass in-sample. It is run
  **once**, with the same criteria b–d, the pinned commit and the same config
  hash.
- The engine refuses OOS without `--oos` and records it in
  `data/_runs/v3/oos_ledger.jsonl`.

## Secondary diagnostics (reported, excluded from pass/fail)

- DP-charge sensitivity: DP = ₹0 and DP doubled. The trades are kept as
  booked, and net is recomputed.
- trades entered from 2020-01-01 onward;
- trades entered on factor-drift stock-days removed;
- the results-day share of entries, and net P&L without those trades;
- regime tables: VIX tercile, expiry weeks, budget weeks (by entry date / week);
- counts of every flag: `EXIT_DEFERRED`, `EXIT_CARRIED`, `EXIT_CORP_ACTION`,
  `EXIT_DELISTED`, `EXIT_NO_MINUTE_DATA`, `EXIT_LOCKED`, `CASH_LIMITED`,
  `QTY_ZERO`, `LOCKED_CIRCUIT`, `ENTRY_MISSING`, `FORCED_END`, dividends with
  no amount, dropped fractional shares, rights ex-dates held through.

Subsets remove trades from the existing book. The ledger is **not** re-run.

## Known limits

1. **Adjustment basis (gap #5).** `P_prev` is adjusted for splits and bonuses
   only, using our factors from `corporate_actions.csv`. Other actions are not
   adjusted.
2. **Dropped fractions (gap #12).** Fractional shares after a split or bonus
   are dropped; in reality they would be paid in cash.
3. **Rights (gap #16).** Rights are never taken up and their value isn't
   credited. The price drop on the ex-date is booked as a loss.
4. **Merger cash-out (gap #17).** A merged stock is closed at its last traded
   close; in reality the holder would receive the acquirer's shares.
5. **Flat exchange rate (gap #18).** 0.00297% throughout, while the real NSE
   rate changed. All CNC rates are unverified.
6. **Same-day sell proceeds.** These fund buys on the same day; T+1
   settlement is ignored.
7. **Whole-day DQ exclusions.** As in V1/V2, a stock-day can be excluded for
   a data problem that shows only after 15:00.
8. **Circuit detection** is the V2 heuristic on 1-min bars, with F&O
   membership from weekly samples.
9. **Taxes** are ignored: results are pre-tax.
10. **Constant DP charge.** ₹15.34 is today's rate, applied across 2018–2024.
    Historical rates differed slightly; the DP sensitivity diagnostic (₹0 and
    2×) covers the range.

## Changes after results

The same rules as V1/V2:
- **Bug fixes only.** Each one is logged in the `# V3` section of
  `docs/CHANGELOG_RESEARCH.md` with its commit hash and before/after values for
  every criterion. `orb backtest --strategy v3` refuses a result-relevant
  commit since the V3 pin that isn't logged there.
- **Any rule change** creates **V3.1**, with its own pre-registration and a
  fresh OOS test.

## Gap resolutions (approved 2026-10-03)

| # | Resolution |
|---|---|
| 1 | Exclusions = V1/V2 DQ errors + special sessions + calendar exceptions; FNO_BAN rows dropped |
| 2 | Corporate-action ex-days exclude ranking and new buys only; held stocks are never forced out by them |
| 3 | A held stock that is DQ-excluded on its sell day → `EXIT_DEFERRED` to the next non-excluded trading day, at the 15:00 open |
| 4 | Warm-up: ≥ 6 prior daily bars and a previous-rebalance close; new index members qualify once they have that history |
| 5 | `P_prev` = official close × F(prev)/F(today), split/bonus only; missing → `NO_PREV_PRICE` |
| 6 | Execution: the 15:00 open, else the first candle 15:01–15:05; a buy with none → `ENTRY_MISSING`; a sell with none → the official close (`EXIT_NO_MINUTE_DATA`) |
| 7 | Sells first, then buys in rank order |
| 8 | `CASH_LIMITED`: qty is reduced until the buy plus its charges (₹1 kept back for rounding) fits the cash |
| 9 | `EXIT_CARRIED`: the next trading day from 09:20, with the guard and a forward search; carries again if locked all day; the carry day's DQ exclusion doesn't block it |
| 10 | Valuation: the 14:59 close, else 14:55–14:58, else the previous official close; excluded held stocks are still valued |
| 11 | Ruin: checked on `equity_w` each rebalance day; no new buys after it; holdings are still sold under the normal rules |
| 12 | Splits/bonuses: qty floored, fraction dropped and counted |
| 13 | Dividends parsed from the subject; no amount → ₹0, counted |
| 14 | Dividends count toward `equity_w`, are reported as their own line, and are included in the trade's net |
| 15 | Demerger: sell at the 15:00 open on the trading day before the ex-date (`EXIT_CORP_ACTION`) |
| 16 | Rights: held through, never taken up, counted |
| 17 | Delisting/merger: the last traded official close (`EXIT_DELISTED`) |
| 18 | New CNC cost profile `costs_cnc.yaml` + `src/orb/v3/costs.py`; V1's cost table untouched; DP once per stock per sell day; day-level STT/stamp rounding |
| 19 | Labels: next-week return from the 15:00 open to the 15:00 open; gross (adjusted, dividends, no costs) drives criterion e; the net version is reported |
| 20 | Null: random selection with the same rules, its own equity path per draw, 2,000 draws, final equity, one-sided |
| 21 | `FORCED_END` at the last rebalance on or before 2024-09-30, paying all costs |
| 22 | Bonferroni over 3 strategies: p < 0.0167 |

## Pre-run decisions (approved 2026-10-03)

These choices were made while building V3, beyond the 22 gap resolutions. They
are rules for this run, and some are also known limits.

| # | Decision |
|---|---|
| D1 | **Ruin is permanent:** once `equity_w < ₹5,000`, no new buys for the rest of the run, even if equity recovers |
| D2 | **`FORCED_END` ignores the circuit guard and the DQ deferral:** everything is sold at the 15:00 open (or the official close if there is no candle), so no position runs past the in-sample end |
| D3 | A target stock with a demerger ex-date on the next trading day is **not bought** (`CORP_ACTION_NEXT_DAY`, counted); the demerger rule would sell it the same day |
| D4 | **Delisting exits** at the last official close pay slippage and the normal sell costs (STT, DP and the rest) |
| D5 | A held stock with **no trading at all** on its sell day (no official bar) is carried to the next trading day |
| D6 | **Criterion e quintiles** are formed within each week; "top" means the highest `r_week` |
| D7 | The **last in-sample week has no label**: computing it would need OOS prices |
| D8 | The **survivorship-gap line** is measured on rebalance days, and the report says so |

## Amendments (all made before any V3 run)

### 2026-10-03: DP charge ₹15.34, verified

- **Before:** ₹15.93 per stock per sell day, unverified.
- **Now:** ₹15.34 = ₹3.50 CDSL + ₹9.50 Zerodha + ₹2.34 GST, from
  zerodha.com/charges at the current rate, marked verified in
  `config/costs_cnc.yaml`.
- **Known limit:** the charge is constant across 2018–2024, although
  historical rates differed slightly. The DP sensitivity diagnostic (₹0 and 2×)
  covers this.
- The golden-week expectations were updated: each sell costs ₹0.59 less.
- **Config hash** changed from `0c918ba0…` to
  `10539881021f7f26f8b25568ed71930f26e21984bfe2310ff01014d2caed5a14`, updated in
  the Identity table above.

