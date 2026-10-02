"""NSE tick sizes.

The tick for trade date T comes from the regime in force on T and the band of
the security's close on the last trading day of the month before T's month. It
applies for that whole month, so a mid-month band crossing changes nothing until
the next month. Prices are raw (actual traded) prices.
"""

from __future__ import annotations

import math
from datetime import date

import polars as pl

from orb.config import TickRegime, TickTable


def regime_on(table: TickTable, day: date) -> TickRegime:
    eligible = [r for r in table.regimes if r.effective_from <= day]
    if not eligible:
        raise ValueError(f"no tick regime in force on {day}")
    return eligible[-1]


def tick_for(table: TickTable, day: date, ref_close: float) -> float:
    return regime_on(table, day).tick_for(ref_close)


def monthly_reference_close(daily: pl.DataFrame) -> pl.DataFrame:
    """(symbol, month, ref_close, ref_date): for each month M in which the symbol
    trades, the last close strictly before M (normally the last trading day of M-1)."""
    month_end = (
        daily.sort("symbol", "date")
        .group_by("symbol", month=pl.col("date").dt.month_start(), maintain_order=True)
        .agg(ref_close=pl.col("close").last(), ref_date=pl.col("date").last())
    )
    months = daily.select("symbol", month=pl.col("date").dt.month_start()).unique()
    # asof: latest month_end strictly before month M  <=>  month_end.month < M
    left = months.with_columns(_key=pl.col("month") - pl.duration(days=1)).sort("_key")
    right = month_end.select("symbol", "ref_close", "ref_date", _key=pl.col("month")).sort("_key")
    return (
        left.join_asof(right, on="_key", by="symbol", strategy="backward", check_sortedness=False)
        .drop("_key")
        .sort("symbol", "month")
    )


def daily_ticks(daily: pl.DataFrame, table: TickTable) -> pl.DataFrame:
    """(symbol, date, ref_close, ref_date, tick) for every row of raw daily bars.
    ``tick`` is null when no earlier close exists (e.g. first month after listing)."""
    ref = monthly_reference_close(daily)
    d = daily.select("symbol", "date", month=pl.col("date").dt.month_start()).join(
        ref, on=["symbol", "month"], how="left"
    )
    ticks = [
        None if rc is None else tick_for(table, day, rc)
        for day, rc in zip(d["date"], d["ref_close"], strict=True)
    ]
    return d.with_columns(tick=pl.Series(ticks, dtype=pl.Float64)).drop("month")


def round_to_tick(price: float, tick: float, side: str) -> float:
    """Adverse rounding: buys round up, sells round down."""
    steps = price / tick
    n = math.ceil(steps - 1e-9) if side == "buy" else math.floor(steps + 1e-9)
    return round(n * tick, 2)
