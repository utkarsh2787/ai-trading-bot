"""V3 schedule, signal and ranking (causal: candles up to 14:59 on the rebalance day).

rebalance day = the last trading day of each calendar (ISO) week
P_now   = close of the 14:59 candle; missing -> last close 14:55..14:58 (substituted)
P_prev  = official close on the previous rebalance day x F(prev)/F(today)
          (split/bonus factors with ex-date in (prev, today])
r_week  = P_now / P_prev - 1
rank    = eligible stocks by r_week ascending (ties: symbol A->Z); target = top 2
"""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl

from orb.features import SessionArrays, slot
from orb.v3.config import ConfigV3
from orb.v3.data import V3Data

EXCLUDED = "EXCLUDED"
NO_MINUTE_DATA = "NO_MINUTE_DATA"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
NO_PREV_PRICE = "NO_PREV_PRICE"
NO_PRICE = "NO_PRICE"
RANKED = "RANKED"

SIGNAL_SCHEMA = {
    "date": pl.Date,
    "symbol": pl.String,
    "prev_rebalance": pl.Date,
    "p_now": pl.Float64,
    "p_now_substituted": pl.Boolean,
    "p_prev_raw": pl.Float64,
    "adj_factor": pl.Float64,
    "p_prev": pl.Float64,
    "r_week": pl.Float64,
    "eligible": pl.Boolean,
    "rank": pl.Int64,
    "target": pl.Boolean,
    "decision": pl.String,
    "reason": pl.String,
}


def rebalance_days(trading_days: list[date]) -> list[date]:
    """Last trading day of each ISO calendar week."""
    last: dict[tuple[int, int], date] = {}
    for d in trading_days:
        iy, iw, _ = d.isocalendar()
        last[(iy, iw)] = max(d, last.get((iy, iw), d))
    return sorted(last.values())


def p_now(a: SessionArrays, cfg: ConfigV3) -> tuple[float | None, bool]:
    """(price, substituted); reads only slots <= the 14:59 candle."""
    s = cfg.session
    k, lo = slot(s, s.signal_candle), slot(s, s.signal_fallback_from)
    if not np.isnan(a.close[k]):
        return float(a.close[k]), False
    for i in range(k - 1, lo - 1, -1):
        if not np.isnan(a.close[i]):
            return float(a.close[i]), True
    return None, False


def scan_rebalance(data: V3Data, day: date, prev: date | None) -> pl.DataFrame:
    """One row per member stock on a rebalance day; eligible rows ranked."""
    cfg = data.cfg
    members = data.members(day)
    data.prepare(members, day)
    rows = []
    for s in members:
        r = dict.fromkeys(SIGNAL_SCHEMA)
        r.update(date=day, symbol=s, prev_rebalance=prev, eligible=False, target=False)
        cat = data.blocks_buy(s, day)
        a = data.session(s, day)
        if cat is not None:
            r.update(decision=EXCLUDED, reason=cat)
        elif a is None:
            r.update(decision=NO_MINUTE_DATA, reason=NO_MINUTE_DATA)
        elif data.bars_before(s, day) < cfg.universe.warmup_sessions:
            r.update(decision=INSUFFICIENT_HISTORY, reason=INSUFFICIENT_HISTORY)
        elif prev is None or data.close(s, prev) is None:
            r.update(decision=NO_PREV_PRICE, reason=NO_PREV_PRICE)
        else:
            price, sub = p_now(a, cfg)
            f = data.factor_between(s, prev, day)
            raw = data.close(s, prev)
            r.update(p_now=price, p_now_substituted=sub, p_prev_raw=raw, adj_factor=f)
            r.update(p_prev=raw * f)
            if price is None:
                r.update(decision=NO_PRICE, reason=NO_PRICE)
            else:
                r.update(
                    r_week=price / (raw * f) - 1,
                    eligible=True,
                    decision=RANKED,
                    reason="P_NOW_SUBSTITUTED" if sub else None,
                )
        rows.append(r)
    return rank(pl.DataFrame(rows, schema=SIGNAL_SCHEMA), cfg.signal.target_size)


def rank(df: pl.DataFrame, top: int) -> pl.DataFrame:
    e = df.filter(pl.col("eligible")).sort(["r_week", "symbol"])
    ranks = {s: i + 1 for i, s in enumerate(e["symbol"].to_list())}
    return df.with_columns(
        rank=pl.col("symbol").replace_strict(ranks, default=None, return_dtype=pl.Int64)
    ).with_columns(target=pl.col("rank").is_not_null() & (pl.col("rank") <= top))
