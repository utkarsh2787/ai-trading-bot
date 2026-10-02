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
