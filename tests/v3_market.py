"""Synthetic multi-week markets for V3 tests.

Daily official bars default to close 100 every weekday; minute bars exist only on
the days given (flat at the day's close unless overridden). Tick 0.01.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl

from orb.context import ContextBuilder
from orb.v3.data import V3Data
from tests.market import bars_from_arrays, weekdays
from tests.v2_market import N, candle, drop, flat  # noqa: F401  (re-exported for tests)

CAL = weekdays(date(2024, 1, 1), 130)  # 2024-01-01 .. 2024-06-28
ACTIONS = {
    "symbol": pl.String,
    "ex_date": pl.Date,
    "action_type": pl.String,
    "price_factor": pl.Float64,
    "subject": pl.String,
}


def daily_frame(symbols, closes: dict, cal=CAL) -> pl.DataFrame:
    """closes: {(symbol, date): close} overrides; default 100. None = no bar."""
    rows = []
    for s in symbols:
        for d in cal:
            c = closes.get((s, d), 100.0)
            if c is None:
                continue
            rows.append((s, d, c, c + 1, c - 1, c, 100_000))
    return pl.DataFrame(
        rows,
        schema={
            "symbol": pl.String,
            "date": pl.Date,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
        orient="row",
    )


def make_data(
    cfg,
    symbols: list[str],
    minute: dict[tuple[str, date], dict],
    closes: dict | None = None,
    excluded: list[tuple[str, date, str]] = (),
    actions: list[tuple] = (),
    cal: list[date] = CAL,
    whole_days: list[date] = (),
    membership: pl.DataFrame | None = None,
    fno=None,
) -> V3Data:
    daily = daily_frame(symbols, closes or {}, cal)
    mins = [
        bars_from_arrays(s, d, b["o"], b["h"], b["l"], b["c"], np.full(N, 1000)).filter(
            pl.Series(~np.isnan(b["c"]))
        )
        for (s, d), b in minute.items()
    ]
    mframe = pl.concat(mins) if mins else pl.DataFrame()

    def load(syms, a, b):
        if mframe.height == 0:
            return mframe
        return mframe.filter(pl.col("symbol").is_in(syms) & pl.col("ts").dt.date().is_between(a, b))

    mem = (
        membership
        if membership is not None
        else pl.DataFrame(
            {"symbol": symbols, "valid_from": cal[0], "valid_to": None},
            schema_overrides={"valid_to": pl.Date},
        )
    )
    ex = pl.DataFrame(
        [(s, d, r) for s, d, r in excluded]
        + [("*", d, "SPECIAL_SESSION_MUHURAT") for d in whole_days],
        schema={"symbol": pl.String, "date": pl.Date, "reason": pl.String},
        orient="row",
    )
    ctx = pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date})
    base = ContextBuilder(cfg, cal, mem, ctx, ex, load, load, set())
    acts = pl.DataFrame(list(actions), schema=ACTIONS, orient="row")
    ticks = {(s, d): 0.01 for s in symbols for d in cal}
    return V3Data(cfg, base, daily, acts, ticks, fno)


def day_bars(close: float = 100.0, **candles) -> dict:
    """Flat minute session at ``close``; candles={"14_59": (o, h, l, c)}."""
    b = flat(close)
    for t, ohlc in candles.items():
        candle(b, t.replace("_", ":"), *ohlc)
    return b
