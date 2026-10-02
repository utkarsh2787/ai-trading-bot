"""Deliverable 5: signal log, feature snapshot and labels.

* signal log   one row per first breakout (taken, skipped, rejected, filtered,
               excluded) for one book (variant x slippage); trade fields only
               for taken trades.
* features     the full causal feature vector at the signal candle close t,
               plus the scorer and config identity, so any future scorer can be
               trained and evaluated on exactly what V1 saw.
* labels       for every first breakout, the V1 trade simulated as if taken
               (sized from its score, ignoring slot limits): net P&L, R multiple
               (P&L / initial risk), MFE / MAE, exit reason. Per-share R is
               always present; rupee labels need qty >= 1 and a score.
"""

from __future__ import annotations

import polars as pl

from orb.signals import FEATURE_KEYS

PRIMARY = ("default", 1.0)

SIGNAL_LOG_COLUMNS = [
    "stock",
    "date",
    "time",
    "side",
    "OR_H",
    "OR_L",
    "ATR14",
    "d",
    "RV",
    "r_idx",
    "w",
    "v",
    "s_breakout",
    "s_rel_volume",
    "s_market",
    "s_or_quality",
    "s_volatility",
    "score",
    "decision",
    "reason",
    "entry",
    "stop",
    "qty",
    "exit",
    "exit_reason",
    "gross_pnl",
    "costs",
    "slippage",
    "net_pnl",
    # extras beyond the spec list
    "net_pnl_rounded",
    "costs_rounded",
    "index_used",
    "exclusion_detail",
    "signal_ts",
]

FEATURE_COLUMNS = [
    "date",
    "symbol",
    "signal_ts",
    "signal_slot",
    "side",
    *[k for k in FEATURE_KEYS if k != "side"],
    "or_h",
    "or_l",
    "or_w",
    "or_candles",
    "close_t",
    "atr14",
    "prev_close",
    "rv_sessions",
    "index_used",
    "index_substituted",
]


def _book(book: pl.DataFrame, variant: str, mult: float) -> pl.DataFrame:
    return book.filter((pl.col("variant") == variant) & (pl.col("slippage_mult") == mult))


def signal_log(
    signals: pl.DataFrame, book: pl.DataFrame, run: tuple[str, float] = PRIMARY
) -> pl.DataFrame:
    b = _book(book, *run).drop("score", "decision", "reason", "side", "signal_slot", strict=False)
    j = signals.join(b, on=["date", "symbol"], how="left")
    taken = pl.col("book_decision") == "TAKEN"

    def trade(col: str) -> pl.Expr:
        return pl.when(taken).then(pl.col(col))

    return j.select(
        stock=pl.col("symbol"),
        date="date",
        time=pl.col("signal_ts").dt.strftime("%H:%M"),
        side="side",
        OR_H="or_h",
        OR_L="or_l",
        ATR14="atr14",
        d="d",
        RV="rv",
        r_idx="r_idx",
        w="w",
        v="v",
        s_breakout="s_breakout",
        s_rel_volume="s_rel_volume",
        s_market="s_market",
        s_or_quality="s_or_quality",
        s_volatility="s_volatility",
        score="score",
        decision=pl.coalesce("book_decision", "decision"),
        reason=pl.when(pl.col("book_decision").is_not_null())
        .then(pl.col("book_reason"))
        .otherwise(pl.col("reason")),
        entry=trade("entry_price"),
        stop=trade("stop"),
        qty=trade("qty"),
        exit=trade("exit_price"),
        exit_reason=trade("exit_reason"),
        gross_pnl=trade("gross_pnl"),
        costs=trade("costs"),
        slippage=trade("slippage_paid"),
        net_pnl=trade("net_pnl"),
        net_pnl_rounded=trade("net_pnl_rounded"),
        costs_rounded=trade("costs_rounded"),
        index_used="index_used",
        exclusion_detail="exclusion_detail",
        signal_ts="signal_ts",
    ).sort("signal_ts", "stock")


def feature_snapshot(signals: pl.DataFrame, scorer_name: str, config_hash: str) -> pl.DataFrame:
    return signals.select(
        *FEATURE_COLUMNS,
        side_sign=pl.when(pl.col("side") == "long").then(1.0).otherwise(-1.0),
        score=pl.col("score"),
        decision="decision",
        reason="reason",
        scorer=pl.lit(scorer_name),
        config_hash=pl.lit(config_hash),
    ).sort("signal_ts", "symbol")


def labels(signals: pl.DataFrame, sims: pl.DataFrame, slippage_mult: float = 1.0) -> pl.DataFrame:
    """One row per first breakout x variant (at ``slippage_mult``)."""
    s = sims.filter(pl.col("slippage_mult") == slippage_mult)
    j = signals.select("date", "symbol", "signal_ts", "side", "score", "decision", "reason").join(
        s, on=["date", "symbol"], how="left"
    )
    return j.select(
        "date",
        "symbol",
        "signal_ts",
        "side",
        "score",
        "decision",
        "reason",
        "variant",
        sim_status=pl.col("status"),
        exit_reason="exit_reason",
        hold_minutes="hold_minutes",
        entry="entry_price",
        stop="stop",
        exit="exit_price",
        risk_per_share="risk_per_share",
        r_per_share_gross=pl.col("gross_per_share") / pl.col("risk_per_share"),
        mfe_r=pl.col("mfe_per_share") / pl.col("risk_per_share"),
        mae_r=pl.col("mae_per_share") / pl.col("risk_per_share"),
        qty="qty",
        initial_risk="initial_risk",
        gross_pnl="gross_pnl",
        costs="costs",
        net_pnl="net_pnl",
        r_gross="r_gross",
        r_net="r_net",
        mfe_rs=pl.col("mfe_per_share") * pl.col("qty"),
        mae_rs=pl.col("mae_per_share") * pl.col("qty"),
    ).sort("signal_ts", "symbol", "variant")
