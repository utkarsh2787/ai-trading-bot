"""Every excluded universe stock-day, with its reason, and a report on them.

Sources: data-quality errors, per-stock reference exclusions (F&O ban,
corporate action ex-dates) and whole-market day exclusions (special sessions,
calendar exceptions) expanded to that day's members. Only point-in-time Nifty
200 member-days on trading days count.
"""

from __future__ import annotations

import polars as pl

from orb.data.quality import ERROR
from orb.data.reference import ReferenceData

EXCL_SCHEMA = {"symbol": pl.String, "date": pl.Date, "reason": pl.String, "source": pl.String}


def universe_days(membership: pl.DataFrame, trading_days: list) -> pl.DataFrame:
    days = pl.DataFrame({"date": trading_days}, schema={"date": pl.Date})
    return (
        membership.join(days, how="cross")
        .filter(
            (pl.col("date") >= pl.col("valid_from"))
            & (pl.col("valid_to").is_null() | (pl.col("date") <= pl.col("valid_to")))
        )
        .select("symbol", "date")
        .unique()
        .sort("date", "symbol")
    )


def exclusion_table(
    dq_issues: pl.DataFrame,
    ref: ReferenceData,
    excluding_action_types: list[str],
    universe: pl.DataFrame,
) -> pl.DataFrame:
    dq = dq_issues.filter(pl.col("severity") == ERROR).select(
        "symbol", "date", reason=pl.col("check").str.to_uppercase(), source=pl.lit("dq")
    )
    stock = ref.stock_day_exclusions(excluding_action_types).with_columns(
        source=pl.lit("reference")
    )
    day = (
        ref.day_exclusions()
        .join(universe, on="date")
        .select("symbol", "date", "reason", source=pl.lit("calendar"))
    )
    allx = pl.concat([dq, stock.select(list(EXCL_SCHEMA)), day.select(list(EXCL_SCHEMA))]).unique()
    return allx.join(universe, on=["symbol", "date"], how="semi").sort("date", "symbol", "reason")


def exclusion_report(
    excl: pl.DataFrame, universe: pl.DataFrame, vix: pl.DataFrame
) -> dict[str, pl.DataFrame]:
    """Tables ``by_reason``, ``by_year``, ``by_vix_tercile``.

    ``stock_days`` counts per reason (a stock-day can have several reasons);
    the ``ALL`` row counts each excluded stock-day once. ``pct`` is relative
    to universe stock-days in the same bucket.
    """

    def tag(df: pl.DataFrame) -> pl.DataFrame:
        return (
            df.with_columns(year=pl.col("date").dt.year())
            .join(vix.select("date", "vix_tercile"), on="date", how="left")
            .with_columns(pl.col("vix_tercile").fill_null("unknown"))
        )

    e = tag(excl)
    u = tag(universe)
    all_rows = e.unique(subset=["symbol", "date"]).with_columns(reason=pl.lit("ALL"))
    e2 = pl.concat([e, all_rows.select(e.columns)])

    def table(keys: list[str]) -> pl.DataFrame:
        counts = e2.group_by([*keys, "reason"]).agg(stock_days=pl.len())
        if keys:
            denom = u.group_by(keys).agg(universe_days=pl.len())
            counts = counts.join(denom, on=keys, how="left")
        else:
            counts = counts.with_columns(universe_days=pl.lit(u.height))
        return counts.with_columns(
            pct=(100 * pl.col("stock_days") / pl.col("universe_days")).round(3)
        ).sort([*keys, "stock_days"], descending=[False] * len(keys) + [True])

    return {
        "by_reason": table([]),
        "by_year": table(["year"]),
        "by_vix_tercile": table(["vix_tercile"]),
    }
