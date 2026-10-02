"""V2 signal and selection at 09:45 (causal: only candles up to 09:44 are read).

    prev_close = official close of the previous session (V1 daily context)
    P_0945     = close of the 09:44 candle (closes at 09:45:00); missing -> last
                 close 09:40..09:43 (P0945_SUBSTITUTED); none -> NO_P0945
    r1         = P_0945 / prev_close - 1
    atr_pct    = ATR14 / prev_close
    z          = r1 / atr_pct;  qualifies if |z| >= z_min;  side = sign(r1)

Selection: qualifying stocks ranked by |z| descending (ties: symbol A->Z), top
``max_positions`` selected. No later additions or replacements.

Index variant (secondary null): z_index from the index's own 09:44 close, prev
close and ATR14; if |z_index| >= z_min, every non-excluded stock with a z is
ranked by alignment = z x sign(r1_index) descending and the top 3 are traded in
the index's direction.
"""

from __future__ import annotations

import math

import numpy as np
import polars as pl

from orb.context import DayInputs
from orb.features import SessionArrays, slot
from orb.v2.config import ConfigV2

EXCLUDED = "EXCLUDED"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
NO_P0945 = "NO_P0945"
BELOW_THRESHOLD = "BELOW_THRESHOLD"
QUALIFIED = "QUALIFIED"

SIGNAL_SCHEMA = {
    "date": pl.Date,
    "symbol": pl.String,
    "prev_close": pl.Float64,
    "p0945": pl.Float64,
    "p0945_substituted": pl.Boolean,
    "r1": pl.Float64,
    "atr14": pl.Float64,
    "atr_pct": pl.Float64,
    "z": pl.Float64,
    "side": pl.String,
    "qualified": pl.Boolean,
    "rank": pl.Int64,
    "selected": pl.Boolean,
    "decision": pl.String,
    "reason": pl.String,
    "exclusion_detail": pl.String,
}


def p0945(a: SessionArrays, cfg: ConfigV2) -> tuple[float | None, bool]:
    """(price, substituted). Reads only slots <= the signal candle."""
    s = cfg.session
    k, lo = slot(s, s.signal_candle), slot(s, s.signal_fallback_from)
    if not np.isnan(a.close[k]):
        return float(a.close[k]), False
    for i in range(k - 1, lo - 1, -1):
        if not np.isnan(a.close[i]):
            return float(a.close[i]), True
    return None, False


def stock_z(prev_close: float, atr14: float, price: float) -> tuple[float, float, float]:
    """(r1, atr_pct, z)."""
    r1 = price / prev_close - 1
    atr_pct = atr14 / prev_close
    return r1, atr_pct, r1 / atr_pct


def scan_day_v2(inputs: DayInputs, cfg: ConfigV2) -> pl.DataFrame:
    """One row per member stock-day with 1-min data (excluded ones included)."""
    rows = []
    for sd in inputs.stocks:
        r = dict.fromkeys(SIGNAL_SCHEMA)
        r.update(date=inputs.day, symbol=sd.symbol, prev_close=sd.prev_close, atr14=sd.atr14)
        r.update(qualified=False, selected=False, exclusion_detail=sd.exclusion_detail)
        if sd.excluded_reason is not None:
            r.update(decision=EXCLUDED, reason=sd.excluded_reason)
        elif sd.atr_reason is not None or not sd.prev_close or not sd.atr14:
            r.update(decision=INSUFFICIENT_HISTORY, reason=sd.atr_reason or "INSUFFICIENT_HISTORY")
        else:
            price, sub = p0945(sd.bars, cfg)
            r.update(p0945=price, p0945_substituted=sub)
            if price is None:
                r.update(decision=NO_P0945, reason=NO_P0945)
            else:
                r1, atr_pct, z = stock_z(sd.prev_close, sd.atr14, price)
                q = abs(z) >= cfg.signal.z_min
                r.update(
                    r1=r1,
                    atr_pct=atr_pct,
                    z=z,
                    side="long" if r1 > 0 else ("short" if r1 < 0 else None),
                    qualified=q,
                    decision=QUALIFIED if q else BELOW_THRESHOLD,
                    reason="P0945_SUBSTITUTED" if sub else None,
                )
        rows.append(r)
    df = pl.DataFrame(rows, schema=SIGNAL_SCHEMA)
    return rank(df, cfg.selection.max_positions)


def rank(df: pl.DataFrame, top: int) -> pl.DataFrame:
    """Rank qualifying rows by |z| desc, symbol A->Z; select the top ``top``."""
    q = df.filter(pl.col("qualified")).sort(
        [pl.col("z").abs(), pl.col("symbol")], descending=[True, False]
    )
    ranks = {s: i + 1 for i, s in enumerate(q["symbol"].to_list())}
    return df.with_columns(
        rank=pl.col("symbol").replace_strict(ranks, default=None, return_dtype=pl.Int64)
    ).with_columns(selected=pl.col("rank").is_not_null() & (pl.col("rank") <= top))


# ----------------------------------------------------------- index variant

INDEX_SCHEMA = {
    "date": pl.Date,
    "index": pl.String,
    "index_substituted": pl.Boolean,
    "prev_close": pl.Float64,
    "p0945": pl.Float64,
    "r1": pl.Float64,
    "atr14": pl.Float64,
    "z": pl.Float64,
    "qualified": pl.Boolean,
    "side": pl.String,
    "reason": pl.String,
}


def index_signal(inputs: DayInputs, index_ctx: dict, cfg: ConfigV2) -> dict:
    """``index_ctx``: {(index name, date): daily-context row} (atr14, prev_close)."""
    out = dict.fromkeys(INDEX_SCHEMA)
    out.update(date=inputs.day, qualified=False)
    ix = inputs.index
    if ix is None:
        return {**out, "reason": "NO_INDEX"}
    out.update(index=ix.name, index_substituted=ix.substituted)
    c = index_ctx.get((ix.name, inputs.day))
    if c is None or c["atr_reason"] is not None or not c["atr14"] or not c["prev_close"]:
        return {**out, "reason": "INSUFFICIENT_HISTORY"}
    price, _ = p0945(ix.bars, cfg)
    if price is None:
        return {**out, "reason": NO_P0945}
    r1, _, z = stock_z(c["prev_close"], c["atr14"], price)
    q = abs(z) >= cfg.index_variant.z_min and r1 != 0
    return {
        **out,
        "prev_close": c["prev_close"],
        "p0945": price,
        "r1": r1,
        "atr14": c["atr14"],
        "z": z,
        "qualified": q,
        "side": ("long" if r1 > 0 else "short") if q else None,
        "reason": None if q else BELOW_THRESHOLD,
    }


def index_selection(signals: pl.DataFrame, idx: dict, top: int) -> pl.DataFrame:
    """Top ``top`` non-excluded stocks by alignment = z x sign(r1_index) (ties: A->Z),
    all on the index's side. Empty when the index doesn't qualify."""
    if not idx.get("qualified"):
        return signals.clear().with_columns(alignment=pl.lit(None, pl.Float64))
    sign = math.copysign(1.0, idx["r1"])
    c = (
        signals.filter(pl.col("z").is_not_null())
        .with_columns(alignment=pl.col("z") * sign)
        .sort([pl.col("alignment"), pl.col("symbol")], descending=[True, False])
        .head(top)
    )
    return c.with_columns(side=pl.lit(idx["side"]))
