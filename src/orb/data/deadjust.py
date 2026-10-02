"""Recover raw (actually traded) prices from a vendor's adjusted series.

Kite returns candles already adjusted for splits, bonuses, rights, spin-offs and
extraordinary dividends, with no raw series, no published factors, and values
that can change on re-fetch. We therefore never trust or re-apply vendor
adjustment. Instead, for every stock-day we take the official NSE bhavcopy
(raw) as ground truth and measure

    ratio(d) = bhav_price(d) / vendor_price(d)       (median over O, H, L, C)

then rebuild raw minute bars as ``vendor * ratio`` and ``volume / ratio``. This
works whatever adjustment method, coverage or date the vendor used (including
none, where ratio == 1). Our own ``orb.data.adjust`` is then applied to the raw
series only, so nothing is adjusted twice.
"""

from __future__ import annotations

import polars as pl

from orb.data.quality import ERROR, ISSUE_SCHEMA, WARN, concat_issues
from orb.data.schema import conform_minute

_PX = ("open", "high", "low", "close")


def deadjust_factors(
    vendor_daily: pl.DataFrame, bhav_daily: pl.DataFrame, tolerance: float
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Returns ``(factors, issues)``; factors = (symbol, date, ratio, dispersion).

    Days whose O/H/L/C ratios disagree by more than ``tolerance`` or that have
    no bhavcopy row are reported as errors (and get no factor).
    """
    j = vendor_daily.join(bhav_daily, on=["symbol", "date"], how="left", suffix="_bhav")
    j = j.with_columns(
        *((pl.col(f"{c}_bhav") / pl.col(c)).alias(f"_r_{c}") for c in _PX)
    ).with_columns(ratio=pl.concat_list([f"_r_{c}" for c in _PX]).list.median())
    j = j.with_columns(
        dispersion=pl.max_horizontal(((pl.col(f"_r_{c}") / pl.col("ratio")) - 1).abs() for c in _PX)
    )
    missing = j.filter(pl.col("close_bhav").is_null())
    noisy = j.filter(pl.col("close_bhav").is_not_null() & (pl.col("dispersion") > tolerance))

    def issues(df: pl.DataFrame, check: str, value: str, detail: str) -> pl.DataFrame:
        if df.height == 0:
            return pl.DataFrame(schema=ISSUE_SCHEMA)
        return df.select(
            "symbol",
            "date",
            check=pl.lit(check),
            severity=pl.lit(ERROR),
            value=pl.col(value).cast(pl.Float64),
            detail=pl.lit(detail),
        )

    all_issues = concat_issues(
        [
            issues(missing, "deadjust_missing_bhav", "close", "no bhavcopy row for vendor bar"),
            issues(
                noisy, "deadjust_inconsistent", "dispersion", "vendor/bhavcopy OHLC ratios disagree"
            ),
        ]
    )
    good = j.filter(pl.col("close_bhav").is_not_null() & (pl.col("dispersion") <= tolerance))
    return good.select("symbol", "date", "ratio", "dispersion").sort("symbol", "date"), all_issues


def deadjust_minute(vendor_minute: pl.DataFrame, factors: pl.DataFrame) -> pl.DataFrame:
    """Vendor-adjusted minute bars -> raw minute bars. Days without a factor are dropped."""
    m = vendor_minute.with_columns(date=pl.col("ts").dt.date()).join(
        factors.select("symbol", "date", "ratio"), on=["symbol", "date"], how="inner"
    )
    m = m.with_columns(
        *((pl.col(c) * pl.col("ratio")).round(2).alias(c) for c in _PX),
        (pl.col("volume") / pl.col("ratio")).round(0).cast(pl.Int64).alias("volume"),
    )
    return conform_minute(m.drop("date", "ratio"))


DRIFT_SCHEMA = {
    "symbol": pl.String,
    "start": pl.Date,
    "end": pl.Date,
    "n_days": pl.Int64,
    "segment_factor": pl.Float64,
    "min_factor": pl.Float64,
    "max_factor": pl.Float64,
}


def factor_drift(
    factors: pl.DataFrame, actions: pl.DataFrame, tolerance: float
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Between two corporate actions the vendor's adjustment factor should be constant.

    Drift is a WARNING (decision 64): the recovered prices on drift days stay within
    0.5% of NSE's official high/low (checked by ``reconcile_daily_minute``, an error
    beyond 0.5%), so drift days are kept and reported, not excluded.

    Segments are delimited by every corporate-action ex-date of the symbol (any
    type: Kite also adjusts for extraordinary dividends). Within a segment the
    reference is the median daily ratio; days off by more than ``tolerance`` are
    DQ errors (``deadjust_factor_drift``). Returns ``(issues, report)``; the
    report has one row per contiguous run of drifting days.
    """
    if factors.height == 0:
        return pl.DataFrame(schema=ISSUE_SCHEMA), pl.DataFrame(schema=DRIFT_SCHEMA)
    ex = actions.select("symbol", "ex_date").unique().sort("ex_date")
    f = factors.sort("symbol", "date")
    # segment id = number of ex-dates <= date (an ex-date starts a new segment)
    seg = f.join_asof(
        ex.with_columns(_n=pl.int_range(1, pl.len() + 1).over("symbol"))
        .rename({"ex_date": "date"})
        .sort("date"),
        on="date",
        by="symbol",
        strategy="backward",
        check_sortedness=False,
    ).with_columns(_seg=pl.col("_n").fill_null(0))
    seg = (
        seg.with_columns(_ref=pl.col("ratio").median().over("symbol", "_seg"))
        .with_columns(_dev=(pl.col("ratio") / pl.col("_ref") - 1).abs())
        .sort("symbol", "date")
    )
    bad = seg.filter(pl.col("_dev") > tolerance)
    issues = (
        bad.select(
            "symbol",
            "date",
            check=pl.lit("deadjust_factor_drift"),
            severity=pl.lit(WARN),
            value=pl.col("ratio"),
            detail=pl.format("segment factor {}", pl.col("_ref").round(6)),
        )
        if bad.height
        else pl.DataFrame(schema=ISSUE_SCHEMA)
    )
    runs = (
        seg.with_columns(_bad=pl.col("_dev") > tolerance)
        .with_columns(
            _run=(pl.col("_bad") != pl.col("_bad").shift(1).over("symbol", "_seg"))
            .fill_null(True)
            .cum_sum()
            .over("symbol")
        )
        .filter(pl.col("_bad"))
        .group_by("symbol", "_seg", "_run")
        .agg(
            start=pl.col("date").min(),
            end=pl.col("date").max(),
            n_days=pl.len().cast(pl.Int64),
            segment_factor=pl.col("_ref").first(),
            min_factor=pl.col("ratio").min(),
            max_factor=pl.col("ratio").max(),
        )
        .select(list(DRIFT_SCHEMA))
        .sort("symbol", "start")
    )
    return issues, runs


# Kite left 1-min VOLUME unadjusted before some older splits/bonuses while
# adjusting prices, so on those days the rebuilt volume is off by the split factor.
VOLUME_FIX_TOLERANCE = 0.05


def later_split_factor(days: pl.DataFrame, actions: pl.DataFrame) -> pl.DataFrame:
    """(date, F): product of split/bonus price factors with ex_date AFTER each date."""
    sb = actions.filter(
        pl.col("action_type").is_in(["split", "bonus"]) & pl.col("price_factor").is_not_null()
    )
    if sb.height == 0:
        return days.select("date").with_columns(F=pl.lit(1.0))
    rows = []
    for d in days["date"].to_list():
        f = 1.0
        for ex, pf in sb.select("ex_date", "price_factor").iter_rows():
            if ex > d:
                f *= pf
        rows.append((d, f))
    return pl.DataFrame(rows, schema={"date": pl.Date, "F": pl.Float64}, orient="row")


def fix_unadjusted_volume(
    minute: pl.DataFrame, bhav: pl.DataFrame, actions: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Divide a day's 1-min volumes by F when, BEFORE the symbol's split/bonus, the
    ratio sum(1-min volume) / bhavcopy quantity is within 5% of the cumulative later
    factor F. Returns (minute, corrections: date, ratio, F)."""
    empty = pl.DataFrame(schema={"date": pl.Date, "ratio": pl.Float64, "F": pl.Float64})
    if minute.height == 0 or bhav.height == 0:
        return minute, empty
    day = minute.group_by(pl.col("ts").dt.date().alias("date")).agg(mvol=pl.col("volume").sum())
    day = day.join(bhav.select("date", "volume"), on="date").filter(pl.col("volume") > 0)
    day = day.join(later_split_factor(day, actions), on="date").with_columns(
        ratio=pl.col("mvol") / pl.col("volume")
    )
    fix = day.filter(
        (pl.col("F") < 0.999) & ((pl.col("ratio") / pl.col("F") - 1).abs() < VOLUME_FIX_TOLERANCE)
    )
    if fix.height == 0:
        return minute, empty
    m = minute.with_columns(date=pl.col("ts").dt.date()).join(
        fix.select("date", "F"), on="date", how="left"
    )
    m = m.with_columns(
        volume=pl.when(pl.col("F").is_not_null())
        .then((pl.col("volume") / pl.col("F")).round(0).cast(pl.Int64))
        .otherwise(pl.col("volume"))
    ).drop("date", "F")
    return m, fix.select("date", "ratio", "F").sort("date")
