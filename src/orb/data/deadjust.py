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

from orb.data.quality import ERROR, ISSUE_SCHEMA, concat_issues
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
