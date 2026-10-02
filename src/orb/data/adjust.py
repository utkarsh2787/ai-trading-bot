"""Split/bonus (and other price-factor) adjustment, point-in-time safe.

For a bar on day d the cumulative factor is

    F(d) = product of price_factor over actions with ex_date > d   (1 if none)

so ``raw(d) * F(d)`` is the series in the *latest* price basis. Intraday
signals and fills use raw prices of the trade date T, so cross-day features for
T must be expressed in T's basis, not the latest one -- otherwise a split
announced after T would leak into T's ATR. The rebasing ratio

    price_T(d)  = raw(d)    * F(d) / F(T)
    volume_T(d) = volume(d) * F(T) / F(d)

only involves actions with d < ex_date <= T, i.e. information known at T.
"""

from __future__ import annotations

import polars as pl


def _factors(actions: pl.DataFrame) -> pl.DataFrame:
    """(symbol, ex_date, cum_factor): product of factors with ex_date >= this one."""
    a = (
        actions.filter(pl.col("price_factor").is_not_null())
        .group_by("symbol", "ex_date")
        .agg(pl.col("price_factor").product())
        .sort("symbol", "ex_date", descending=[False, True])
        .with_columns(cum_factor=pl.col("price_factor").cum_prod().over("symbol"))
        .select("symbol", "ex_date", "cum_factor")
        .sort("symbol", "ex_date")
    )
    return a


def with_adj_factor(
    df: pl.DataFrame, actions: pl.DataFrame, date_col: str = "date"
) -> pl.DataFrame:
    """Add ``adj_factor`` = F(d) for each row's (symbol, date)."""
    fac = _factors(actions).with_columns(pl.col("ex_date").alias("_key"))
    left = df.with_columns(
        _key=pl.col(date_col) + pl.duration(days=1),  # first ex_date >= d+1  <=>  ex_date > d
        _row=pl.int_range(pl.len()),
    ).sort("_key")
    # both sides are globally sorted on _key, hence also within each symbol
    out = left.join_asof(
        fac.sort("_key"), on="_key", by="symbol", strategy="forward", check_sortedness=False
    )
    return (
        out.with_columns(adj_factor=pl.col("cum_factor").fill_null(1.0))
        .sort("_row")
        .drop("_key", "_row", "cum_factor", "ex_date")
    )


def adjust_daily(daily: pl.DataFrame, actions: pl.DataFrame) -> pl.DataFrame:
    """Daily bars plus ``adj_*`` columns in the latest price basis."""
    d = with_adj_factor(daily, actions)
    return d.with_columns(
        *(
            (pl.col(c) * pl.col("adj_factor")).alias(f"adj_{c}")
            for c in ("open", "high", "low", "close")
        ),
        adj_volume=pl.col("volume") / pl.col("adj_factor"),
    )


def rebase_price(adj_price: pl.Expr | float, factor_at_t: pl.Expr | float):
    """Latest-basis price -> price in the basis of trade date T (see module doc)."""
    return adj_price / factor_at_t


def rebase_volume(adj_volume: pl.Expr | float, factor_at_t: pl.Expr | float):
    return adj_volume * factor_at_t
