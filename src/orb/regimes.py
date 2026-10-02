"""Regime tags shared by the exclusion report and the backtest reports."""

from __future__ import annotations

from datetime import date

import polars as pl

TERCILES = ["low", "mid", "high"]


def vix_terciles(
    vix_daily: pl.DataFrame, in_sample_start: date, in_sample_end: date
) -> pl.DataFrame:
    """(date, vix_prev_close, vix_tercile) for every VIX trading day.

    Uses the PREVIOUS day's India VIX close (known before the open). Cut points
    are the 1/3 and 2/3 quantiles over the in-sample period only, so OOS days
    are bucketed with in-sample thresholds.
    """
    v = vix_daily.sort("date").select("date", vix_prev_close=pl.col("close").shift(1))
    ins = v.filter(pl.col("date").is_between(in_sample_start, in_sample_end))[
        "vix_prev_close"
    ].drop_nulls()
    if ins.len() == 0:
        return v.with_columns(vix_tercile=pl.lit(None, pl.String))
    q1, q2 = ins.quantile(1 / 3, "linear"), ins.quantile(2 / 3, "linear")
    return v.with_columns(
        vix_tercile=pl.when(pl.col("vix_prev_close").is_null())
        .then(None)
        .when(pl.col("vix_prev_close") <= q1)
        .then(pl.lit("low"))
        .when(pl.col("vix_prev_close") <= q2)
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
