"""Data-quality checks.

Every check emits rows ``(symbol, date, check, severity, value, detail)``.
Any ``error`` excludes that stock-day from the backtest; ``warn`` is reported only.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from orb.config import DQConfig, SessionConfig

ISSUE_SCHEMA = {
    "symbol": pl.String(),
    "date": pl.Date(),
    "check": pl.String(),
    "severity": pl.String(),
    "value": pl.Float64(),
    "detail": pl.String(),
}

WARN, ERROR = "warn", "error"


def _issues(df: pl.DataFrame, check: str, severity: str | pl.Expr, value: pl.Expr, detail: str):
    if df.height == 0:
        return pl.DataFrame(schema=ISSUE_SCHEMA)
    sev = severity if isinstance(severity, pl.Expr) else pl.lit(severity)
    return df.select(
        pl.col("symbol"),
        pl.col("date"),
        pl.lit(check).alias("check"),
        sev.alias("severity"),
        value.cast(pl.Float64).alias("value"),
        pl.lit(detail).alias("detail"),
    )


def _minutes(t) -> int:
    return t.hour * 60 + t.minute


def check_minute(
    df: pl.DataFrame,
    dq: DQConfig,
    session: SessionConfig,
    is_index: bool = False,
    special_dates: set[date] | frozenset[date] = frozenset(),
) -> pl.DataFrame:
    """Checks on canonical minute bars (one or many symbols)."""
    if df.height == 0:
        return pl.DataFrame(schema=ISSUE_SCHEMA)
    first, last = _minutes(session.first_candle), _minutes(session.last_candle)
    d = df.with_columns(
        date=pl.col("ts").dt.date(),
        _mod=pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32),
        _sec=pl.col("ts").dt.second() + pl.col("ts").dt.microsecond(),
    )
    out: list[pl.DataFrame] = []

    # --- null / non-positive prices / OHLC consistency (row level)
    px = ["open", "high", "low", "close"]
    rows = d.group_by("symbol", "date").agg(
        nulls=pl.any_horizontal(pl.col(c).is_null() for c in [*px, "volume"]).sum(),
        nonpos=pl.any_horizontal(pl.col(c) <= 0 for c in px).sum(),
        ohlc_bad=(
            (pl.col("high") < pl.max_horizontal("open", "close"))
            | (pl.col("low") > pl.min_horizontal("open", "close"))
            | (pl.col("low") > pl.col("high"))
        ).sum(),
        off_minute=(pl.col("_sec") != 0).sum(),
        dup=pl.len() - pl.col("ts").n_unique(),
        outside=((pl.col("_mod") < first) | (pl.col("_mod") > last)).sum(),
        in_session=pl.col("ts")
        .filter((pl.col("_mod") >= first) & (pl.col("_mod") <= last) & (pl.col("_sec") == 0))
        .n_unique(),
        first_mod=pl.col("_mod").min(),
        last_mod=pl.col("_mod").max(),
        neg_vol=(pl.col("volume") < 0).sum(),
        zero_vol_range=((pl.col("volume") == 0) & (pl.col("high") > pl.col("low"))).sum(),
        max_vol=pl.col("volume").max(),
        med_vol=pl.col("volume").median(),
    )
    for col, check, detail in [
        ("nulls", "null_values", "candles with null OHLCV"),
        ("nonpos", "nonpositive_price", "candles with price <= 0"),
        ("ohlc_bad", "ohlc_inconsistent", "high/low do not bound open/close"),
        ("off_minute", "off_minute_timestamp", "timestamps not on a minute boundary"),
        ("dup", "duplicate_timestamp", "duplicate candle timestamps"),
        ("outside", "outside_session", "candles outside session (tz shift?)"),
    ]:
        out.append(_issues(rows.filter(pl.col(col) > 0), check, ERROR, pl.col(col), detail))

    # --- session boundaries and completeness (skipped for known special sessions)
    regular = rows.filter(~pl.col("date").is_in(list(special_dates)))
    out.append(
        _issues(
            regular.filter(pl.col("first_mod") > first),
            "late_start",
            WARN,
            pl.col("first_mod") - first,
            "first candle after session open (minutes late)",
        )
    )
    out.append(
        _issues(
            regular.filter(pl.col("last_mod") < last),
            "early_end",
            WARN,
            last - pl.col("last_mod"),
            "last candle before session close (minutes early)",
        )
    )
    missing = regular.with_columns(_miss=dq.expected_candles - pl.col("in_session")).filter(
        pl.col("_miss") > dq.max_missing_minutes_warn
    )
    out.append(
        _issues(
            missing,
            "missing_minutes",
            pl.when(pl.col("_miss") > dq.max_missing_minutes_error)
            .then(pl.lit(ERROR))
            .otherwise(pl.lit(WARN)),
            pl.col("_miss"),
            "in-session minutes without a candle",
        )
    )

    # --- volume (indices carry no volume on Kite)
    if not is_index:
        out.append(
            _issues(
                rows.filter(pl.col("neg_vol") > 0),
                "negative_volume",
                ERROR,
                pl.col("neg_vol"),
                "candles with volume < 0",
            )
        )
        out.append(
            _issues(
                rows.filter(pl.col("zero_vol_range") > 0),
                "zero_volume_with_range",
                WARN,
                pl.col("zero_vol_range"),
                "volume 0 but high > low",
            )
        )
        spike = rows.filter(
            (pl.col("med_vol") > 0) & (pl.col("max_vol") > dq.volume_spike_mult * pl.col("med_vol"))
        )
        out.append(
            _issues(
                spike,
                "volume_spike",
                WARN,
                pl.col("max_vol") / pl.col("med_vol"),
                "max candle volume / session median",
            )
        )
        out.append(
            _issues(
                rows.filter(pl.col("max_vol") == 0),
                "no_volume_day",
                ERROR,
                pl.col("max_vol"),
                "session with zero total volume",
            )
        )
    return concat_issues(out)


def check_daily(
    daily: pl.DataFrame, actions: pl.DataFrame, dq: DQConfig, is_index: bool = False
) -> pl.DataFrame:
    """Checks on canonical daily bars."""
    if daily.height == 0:
        return pl.DataFrame(schema=ISSUE_SCHEMA)
    out: list[pl.DataFrame] = []
    dups = daily.group_by("symbol", "date").agg(n=pl.len()).filter(pl.col("n") > 1)
    out.append(_issues(dups, "duplicate_date", ERROR, pl.col("n"), "duplicate daily bars"))
    d = daily.unique(subset=["symbol", "date"], keep="last").sort("symbol", "date")
    bad = d.filter(
        (pl.col("high") < pl.max_horizontal("open", "close"))
        | (pl.col("low") > pl.min_horizontal("open", "close"))
        | pl.any_horizontal(pl.col(c) <= 0 for c in ["open", "high", "low", "close"])
    )
    out.append(_issues(bad, "ohlc_inconsistent", ERROR, pl.lit(1), "bad daily OHLC"))
    if not is_index:
        out.append(
            _issues(
                d.filter(pl.col("volume") <= 0),
                "zero_volume",
                WARN,
                pl.col("volume"),
                "daily volume <= 0",
            )
        )

    # unexplained overnight gap: no corporate action of any kind on that ex_date
    gaps = d.with_columns(gap=pl.col("open") / pl.col("close").shift(1).over("symbol") - 1).filter(
        pl.col("gap").abs() > dq.overnight_gap_error
    )
    explained = actions.select("symbol", pl.col("ex_date").alias("date")).unique()
    gaps = gaps.join(explained, on=["symbol", "date"], how="anti")
    out.append(
        _issues(
            gaps,
            "unexplained_gap",
            ERROR,
            pl.col("gap"),
            "overnight gap beyond threshold with no corporate action",
        )
    )
    return concat_issues(out)


def reconcile_daily_minute(daily: pl.DataFrame, minute: pl.DataFrame, dq: DQConfig) -> pl.DataFrame:
    """Daily high/low vs aggregated minute high/low (raw basis)."""
    if daily.height == 0 or minute.height == 0:
        return pl.DataFrame(schema=ISSUE_SCHEMA)
    agg = minute.group_by("symbol", date=pl.col("ts").dt.date()).agg(
        m_high=pl.col("high").max(), m_low=pl.col("low").min()
    )
    j = daily.join(agg, on=["symbol", "date"], how="inner").with_columns(
        diff=pl.max_horizontal(
            (pl.col("high") / pl.col("m_high") - 1).abs(),
            (pl.col("low") / pl.col("m_low") - 1).abs(),
        )
    )
    return _issues(
        j.filter(pl.col("diff") > dq.daily_minute_tolerance),
        "daily_minute_mismatch",
        WARN,
        pl.col("diff"),
        "daily H/L differs from minute H/L",
    )


def concat_issues(parts: list[pl.DataFrame]) -> pl.DataFrame:
    parts = [p for p in parts if p.height]
    if not parts:
        return pl.DataFrame(schema=ISSUE_SCHEMA)
    return pl.concat(parts).sort("symbol", "date", "check")


def excluded_stock_days(issues: pl.DataFrame) -> pl.DataFrame:
    """Unique ``(symbol, date)`` with at least one error."""
    return (
        issues.filter(pl.col("severity") == ERROR)
        .select("symbol", "date")
        .unique()
        .sort("symbol", "date")
    )
