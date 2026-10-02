"""Canonical bar schemas shared by every provider, the store and the DQ checks."""

from __future__ import annotations

import polars as pl

TZ = "Asia/Kolkata"

MINUTE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String(),
    "ts": pl.Datetime("us", TZ),  # candle START time, IST
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Int64(),
}

DAILY_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String(),
    "date": pl.Date(),
    "open": pl.Float64(),
    "high": pl.Float64(),
    "low": pl.Float64(),
    "close": pl.Float64(),
    "volume": pl.Int64(),
}


def empty(schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def _localize_ts(df: pl.DataFrame) -> pl.DataFrame:
    dtype = df.schema["ts"]
    if dtype == pl.String:
        df = df.with_columns(pl.col("ts").str.to_datetime(time_unit="us"))
        dtype = df.schema["ts"]
    if isinstance(dtype, pl.Datetime) and dtype.time_zone is None:
        df = df.with_columns(pl.col("ts").dt.replace_time_zone(TZ))
    return df.with_columns(pl.col("ts").dt.convert_time_zone(TZ).dt.cast_time_unit("us"))


def conform_minute(df: pl.DataFrame) -> pl.DataFrame:
    """Select, cast and sort to MINUTE_SCHEMA. Naive timestamps are taken as IST."""
    if df.height == 0:
        return empty(MINUTE_SCHEMA)
    df = _localize_ts(df)
    return df.select(pl.col(c).cast(t) for c, t in MINUTE_SCHEMA.items()).sort("symbol", "ts")


def conform_daily(df: pl.DataFrame) -> pl.DataFrame:
    if df.height == 0:
        return empty(DAILY_SCHEMA)
    dtype = df.schema["date"]
    if isinstance(dtype, pl.Datetime):
        if dtype.time_zone is not None:
            df = df.with_columns(pl.col("date").dt.convert_time_zone(TZ))
        df = df.with_columns(pl.col("date").dt.date())
    elif dtype == pl.String:
        df = df.with_columns(pl.col("date").str.to_date())
    return df.select(pl.col(c).cast(t) for c, t in DAILY_SCHEMA.items()).sort("symbol", "date")
