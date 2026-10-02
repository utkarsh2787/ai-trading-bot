# Pre-registration: V2, intraday time-series momentum

Frozen on 2026-10-02, **before any V2 run** on any data. V2 results are judged
only against what is written here. V1 is closed as FAIL (see
`docs/PREREGISTRATION.md` and run `20261002T213846_0822eb04`); its records
don't change.

## Identity

| | |
|---|---|
| Spec version | **V2** (the V2 spec of 2026-10-02, with the gap resolutions below) |
| Strategies tested so far | **2** (V1, V2). Bonferroni: criterion c uses p < 0.05 / 2 = **0.025** |
| Code commit | **pinned automatically by the first V2 in-sample run**, in its `meta.json` and in `data/_runs/v2/insample_pin.json` |
| Config hash | `ac6b5b549c9716588246fa93f31d5c4f8093467709db693e82ff41b9246e94c7` (`config/v2.yaml` + tick and cost tables, including the `prereg` thresholds) |
| In-sample period | 2018-01-01 to 2024-09-30 |
| OOS start | 2024-10-01 (never run so far, by any strategy) |
| Data | The frozen Kite snapshot `20261002T143725`, the raw store and the reference files used by V1, and the DQ exclusions in `data/_dq`. No new downloads. Each run records its data version. |

## Hypothesis

Intraday time-series momentum (Gao, Han, Li and Zhou 2018, adapted to single
stocks): the return from the previous close to 09:45 predicts the direction of
the late session.

## Universe and data

The same as V1:

- point-in-time Nifty 200 membership;
- the same exclusions: DQ errors (including `volume_mismatch`), F&O ban,
  corporate-action days, special sessions and calendar exceptions;
- budget days are traded and tagged.

The only warm-up rule is the ATR one: at least 100 daily bars (gap #4). V2 uses
no volume baseline, so V1's 1-min volume warm-up doesn't apply.

## Signal (once per day, at 09:45, causal)

Candles are labelled by their start time, so the 09:44 candle closes at
09:45:00 (gap #1).

| | |
|---|---|
| `prev_close` | official NSE close of the previous session (bhavcopy), on the trade date's price basis; the same value as V1 (gap #3) |
| `P_0945` | close of the 09:44 candle. If it is missing: the last close from 09:40 to 09:43, flagged `P0945_SUBSTITUTED`. If there is none: `NO_P0945`, not ranked (gap #2) |
| `r1` | `P_0945 / prev_close − 1` |
| `atr_pct` | `ATR14 / prev_close` (Wilder ATR14 as in V1: daily bars up to the previous day, special sessions excluded) |
| `z` | `r1 / atr_pct` |
| qualifies | `|z| ≥ 0.25` |
| direction | `sign(r1)`: long if positive, short if negative |

## Selection

- Only qualifying stocks are ranked, by `|z|` descending at 09:45. Ties are
  broken by symbol, A→Z (gap #7).
- The top 3 are taken. There are no later additions or replacements that day.

## Entry and exit

**Entry.** The open of the 14:30 candle, plus slippage.
- If the 14:30 candle is missing, the open of the first candle from 14:31 to
  14:35.
- If there is none, the trade is skipped and logged `ENTRY_MISSING`. The slot
  stays empty; rank 4 is not promoted (gap #8).

**Exit.** The open of the 15:10 candle, minus slippage.
- If the 15:10 candle is missing, the close of the last available candle
  before 15:10, flagged `EXIT_SUBSTITUTED` and counted in the report (gap #9).

There is no stop loss and no target. At most one trade per stock per day.

**Circuit-lock guard (gap #21).** It applies only to stocks **not in F&O** on
that date.
- **Locked candle.** A candle counts as locked for an order when it has
  `high == low`, and that price equals:
  - for a **buy** order: the day's high so far (from 09:15, including that
    candle), i.e. an upper circuit;
  - for a **sell** order: the day's low so far, i.e. a lower circuit.

  Entry: a long buys and a short sells. Exit: a long sells and a short buys.
  For the entry this matches the spec (high so far for a long, low so far for
  a short). For the exit, "the same check" is applied to the side of the exit
  order, because that is the side the lock blocks.
- **Entry.** If the entry candle (the 14:30 candle, or its fallback) is
  locked, the entry is skipped and logged `LOCKED_CIRCUIT`. The slot stays
  empty, and the count is reported.
- **Exit.** If the 15:10 candle is locked, the trade exits at the first
  unlocked candle found going back from 15:10, at that candle's close. This
  mirrors `EXIT_SUBSTITUTED`. If no candle after the entry is unlocked, it
  exits at the 15:10 open. Both cases are flagged `EXIT_LOCKED` and counted.
- **F&O membership per date.** It is derived from the F&O bhavcopies already
  cached (weekly samples; no new downloads). A stock is in F&O on date d if a
  stock futures contract (`FUTSTK` / `STF`) is listed in **both** weekly
  samples bracketing d (or the last sample, at the end of the data), or if the
  stock is on the F&O ban list that day. A transition week therefore counts
  as non-F&O, so the guard applies.

## Sizing (tracks equity, unlike V1)

| | |
|---|---|
| Starting capital | ₹10,000 |
| `equity_d` | ₹10,000 + cumulative **rupee-rounded** net P&L up to the previous day (gap #13) |
| `Deployable_d` | 80% × `equity_d` |
| `Slot_d` | `Deployable_d / 3`, equal weight (no score weighting) |
| `qty` | `floor(Slot_d / entry fill price)`, slippage included (gap #12). `qty < 1` → skip, `QTY_ZERO` |
| Ruin stop | no new trades once `equity_d < ₹5,000`. The ruin date is the first such day. With no new trades it is permanent (gap #14) |
| Leverage | none. Entry charges come out of the 20% not deployed; cash is checked never to go negative |

Every slippage book has its **own** equity path, quantities and ruin date
(gap #11).

## Slippage

| Book | Per fill, against the trade | Role |
|---|---|---|
| `primary` | 1 tick, from the date-keyed monthly tick table. No percentage and no rounding (gap #10: a raw open slightly off the tick grid stays off it) | **primary book**; criteria a–e |
| `primary_2x` | 2 ticks, no rounding | reported only |
| `v1_model` | V1 model: 1 tick + 0.02% of price, rounded against the trade to the tick | **stress book**; criterion b |

## Costs

The same Zerodha cost module as V1, including per-day rupee rounding of STT
and stamp duty (contract note), computed per book. Results are reported with
and without rounding.

## Logging

- **Signal log.** One row for every stock-day at 09:45, with `symbol, date,
  prev_close, P_0945, r1, atr_pct, z, qualified, rank, decision, reason`.
  Excluded stock-days are logged too, with decision `EXCLUDED` and no z or
  rank, so coverage can be audited (gap #6).
- **Taken trades** also log `entry, exit, qty, gross, costs, slippage, net,
  exit reason` (and the rounded costs and net).
- **Labels.** Every **qualifying** stock (not only the top 3) is simulated as
  if taken, at that day's `Slot_d`, under primary slippage (gap #18).

## Nulls

**Primary null (criterion c).** Random direction per trade, with the same
stocks, times and qty.
- Only the raw price move is flipped. The twin's gross is
  `−side × (raw exit − raw entry) × qty − the actual trade's slippage`, and the
  twin pays the actual trade's rupee costs. Slippage and costs stay costs
  (gap #15).
- The actual-book statistic uses the **same unrounded costs** as the twins,
  so the comparison is like-for-like.
- 2,000 fair-coin draws, seed 20180101, one-sided p = P(random total ≥ actual
  total). Passes at **p < 0.025**.
- The quantities are those of the actual book, so the null has no equity
  feedback.

**Secondary (reported, not pass/fail):**
- **Always-long baseline:** the same selected trades, long, with the same qty
  and costs (gap #16).
- **Index-level variant** (approved change to gap #17):
  - Compute `z_index` for the Nifty 200 index from its 09:44 close, its
    previous close and its own ATR14, using the same index source and
    fallback as V1.
  - If `|z_index| ≥ 0.25`, rank all non-excluded stocks by
    `alignment = z × sign(r1_index)`, descending, and take the top 3. All are
    traded in the index's direction.
  - If the index doesn't qualify, there are no trades that day.
  - The variant has its own equity path, with V2 sizing, entry, exit and the
    circuit guard.

## Success criteria (in-sample 2018-01-01 to 2024-09-30)

| | Criterion | Measured on |
|---|---|---|
| a | ≥ 300 taken trades | primary book |
| b | net P&L (rupee-rounded) > 0 under primary slippage **and** ≥ 0 under the V1-model stress | `primary` and `v1_model` books, each with its own equity path |
| c | beats the random-direction null at **p < 0.025** (one-sided; Bonferroni over 2 strategies) | primary book, unrounded costs on both sides |
| d | net positive (rupee-rounded) in ≥ 60% of calendar years (2018–2024, partial 2024 counted, as in V1) **and** the ruin stop is never hit | primary book |
| e | *informational:* mean net return per trade (net / notional) rises strictly across the `|z|` terciles of qualifying stocks | labels (every qualifying stock, primary slippage); tercile cut points over all in-sample qualifying labels |

**Gate:** a–d must all pass in-sample. Criterion e doesn't gate.

`orb report --strategy v2` prints one `[PASS]`/`[FAIL]` line per criterion and
the gate verdict at the top of `report.md`. Then come the headline metrics, the
nulls and the secondary diagnostics. The thresholds live in `config/v2.yaml`
under `prereg` and are covered by the config hash above.

## Out-of-sample

- OOS (2024-10-01 onward) is allowed only if a–d pass in-sample. It is run
  **once**, with the same criteria b–d, the pinned commit and the same config
  hash.
- The engine refuses OOS without `--oos` and records it in
  `data/_runs/v2/oos_ledger.jsonl`.

## Secondary diagnostics (reported, excluded from pass/fail)

- drift stock-days excluded;
- 2020-01-01 onward;
- long vs short split;
- results days: the share of V2 trades on results days, and headline metrics
  with results-day trades removed;
- regime tables as in V1: VIX tercile, trend/range, expiry, results, budget;
- counts of `LOCKED_CIRCUIT`, `EXIT_LOCKED`, `EXIT_SUBSTITUTED`,
  `ENTRY_MISSING`, `QTY_ZERO` and `P0945_SUBSTITUTED`.

Trades are removed from the existing book. The equity path is **not** re-run,
so quantities stay as booked.

## Known limits

1. **Whole-day exclusions (gap #5).** DQ errors are judged on the whole day,
   for example a full-day high/low or volume mismatch. A stock-day can
   therefore be excluded for a data problem that shows only after 09:45. V1
   had the same limit. Volume errors (0.98% of stock-days) exclude V2 days
   although V2 doesn't use volume, because the spec keeps the same
   exclusions.
2. **Circuit detection is a heuristic (gap #21).** It works on 1-min bars
   only, not on the order book or NSE price bands:
   - it uses the entry or exit candle's own high and low, i.e. that whole
     minute;
   - a locked candle that isn't at the day's extreme is not caught;
   - F&O membership comes from weekly samples;
   - the backward exit substitution on `EXIT_LOCKED` uses an earlier price.

   Real fills at a locked circuit would be no better, and often worse.
3. **Primary slippage of 1 tick** is an assumption, not a measurement. The
   V1-model stress book is the check on it.

## Changes after results

The same rules as V1:
- **Bug fixes only.** Each one is logged in the `# V2` section of
  `docs/CHANGELOG_RESEARCH.md` with its commit hash and before/after values for
  every criterion. `orb backtest --strategy v2` refuses a result-relevant
  commit since the V2 pin that isn't logged there.
- **Any rule change** creates **V2.1**, with its own pre-registration and a
  fresh OOS test.

## Gap resolutions (approved 2026-10-02)

The numbers refer to the V2 gap list. The changes made at approval are #15,
#17 and #21.

| # | Resolution |
|---|---|
| 1 | The 09:44 candle closes at 09:45:00; the signal uses only past data |
| 2 | The 09:44 candle is missing → the last close from 09:40 to 09:43 (`P0945_SUBSTITUTED`); none → `NO_P0945` |
| 3 | `prev_close` = V1's official close, on the trade date's basis |
| 4 | ATR warm-up only (≥ 100 daily bars); no volume-baseline warm-up |
| 5 | Whole-day DQ exclusions are kept, volume errors included (known limit 1) |
| 6 | Excluded stock-days are logged as `EXCLUDED`, not ranked |
| 7 | Only qualifying stocks are ranked |
| 8 | Entry fallback 14:31–14:35; otherwise `ENTRY_MISSING`, the slot stays empty |
| 9 | `EXIT_SUBSTITUTED`: the close of the last candle before 15:10 |
| 10 | Primary slippage is not rounded to the tick grid |
| 11 | Each slippage book has its own equity path |
| 12 | qty is computed from the entry fill price, slippage included |
| 13 | Equity follows rupee-rounded net; the unrounded figures use the same trades |
| 14 | Ruin: the first day `equity_d < ₹5,000`, permanent; criterion d tests the primary book |
| 15 | Null: only the raw move is flipped; slippage and costs stay costs; the actual statistic uses the same unrounded costs; one-sided, 2,000 draws, p < 0.025 |
| 16 | Always-long baseline: the same trades, qty and costs |
| 17 | Index variant: `|z_index| ≥ 0.25` → the top 3 non-excluded stocks by `z × sign(r1_index)`, traded in the index's direction; its own equity path |
| 18 | Criterion e: net / notional; labels at `Slot_d` under primary slippage; terciles over all in-sample qualifying labels; strictly rising |
| 19 | Calendar years 2018–2024, partial 2024 included |
| 20 | Primary book = `primary` slippage with rupee-rounded costs; Bonferroni over 2 strategies |
| 21 | Circuit-lock guard, as specified above; heuristic (known limit 2) |
