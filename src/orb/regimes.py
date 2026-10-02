"""Regime tags shared by the exclusion report and the backtest reports."""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl

TERCILES = ["low", "mid", "high"]


def vix_terciles(vix_daily: pl.DataFrame, min_history: int) -> pl.DataFrame:
    """(date, vix_prev_close, q1, q2, vix_tercile) for every VIX trading day.

    Causal: on day d the tagged value is the PREVIOUS day's India VIX close, and
    the tercile cut points (1/3 and 2/3 quantiles) are taken over every close up
    to and including that previous day (expanding window, never future data).
    Days with fewer than ``min_history`` prior closes are untagged (null).
    """
    v = vix_daily.sort("date")
    closes = v["close"].to_numpy().astype(float)
    n = closes.size
    prev = np.full(n, np.nan)
    q1 = np.full(n, np.nan)
    q2 = np.full(n, np.nan)
    for i in range(1, n):
        hist = closes[:i]  # closes up to d-1
        prev[i] = closes[i - 1]
        if hist.size >= min_history:
            q1[i], q2[i] = np.quantile(hist, [1 / 3, 2 / 3])
    out = (
        v.select("date")
        .with_columns(vix_prev_close=pl.Series(prev), q1=pl.Series(q1), q2=pl.Series(q2))
        .with_columns(pl.col("vix_prev_close", "q1", "q2").fill_nan(None))
    )
    return out.with_columns(
        vix_tercile=pl.when(pl.col("q1").is_null())
        .then(None)
        .when(pl.col("vix_prev_close") <= pl.col("q1"))
        .then(pl.lit("low"))
        .when(pl.col("vix_prev_close") <= pl.col("q2"))
        .then(pl.lit("mid"))
        .otherwise(pl.lit("high"))
    )


def results_days(results: pl.DataFrame, calendar: list[date]) -> pl.DataFrame:
    """(symbol, date) tagged results_day: the board-meeting date itself (if a
    trading day) and the next trading day after it (results usually land after
    the close)."""
    cal = pl.DataFrame({"date": calendar}, schema={"date": pl.Date}).sort("date")
    meet = results.select("symbol", "date").unique()
    same = meet.join(cal, on="date", how="semi")
    nxt = (
        meet.with_columns(_k=pl.col("date") + pl.duration(days=1))
        .sort("_k")
        .join_asof(
            cal.with_columns(_d=pl.col("date")).select("_d"),
            left_on="_k",
            right_on="_d",
            strategy="forward",
            check_sortedness=False,
        )
        .select("symbol", date=pl.col("_d"))
        .drop_nulls()
    )
    return pl.concat([same, nxt]).unique().sort("symbol", "date")


def expiry_flags(expiries: pl.DataFrame) -> pl.DataFrame:
    """date -> one boolean column per expiry type plus ``is_expiry``."""
    if expiries.height == 0:
        return pl.DataFrame(schema={"date": pl.Date, "is_expiry": pl.Boolean})
    wide = (
        expiries.select("date", "expiry_type", _v=pl.lit(True))
        .unique()
        .pivot(on="expiry_type", index="date", values="_v")
    )
    types = [c for c in wide.columns if c != "date"]
    return (
        wide.with_columns(*(pl.col(c).fill_null(False).alias(f"is_{c}") for c in types))
        .select("date", *(f"is_{c}" for c in types))
        .with_columns(is_expiry=pl.any_horizontal(f"is_{c}" for c in types))
        .sort("date")
    )


def trend_days(index_daily: pl.DataFrame, threshold: float) -> pl.DataFrame:
    """Same-day tag for reporting: |close - open| / (high - low) >= threshold."""
    rng = pl.col("high") - pl.col("low")
    return index_daily.select(
        "date",
        trend_day=pl.when(rng > 0)
        .then((pl.col("close") - pl.col("open")).abs() / rng >= threshold)
        .otherwise(False),
    ).sort("date")
