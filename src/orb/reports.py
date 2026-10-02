"""Deliverable 6: report generator.

Outputs (``<run>/report/``):
  trade_log.csv         every taken trade, every (variant, slippage) book
  signal_log.csv        every first breakout (primary book)
  summary.csv           per book: counts, hit rate, avg win / loss, gross and net
                        separately (net with and without contract-note rounding),
                        slippage paid, hold time, R, drawdown
  regime_<tag>.csv      primary book by India VIX tercile, trend vs range day,
                        expiry vs non-expiry (and by type), results day
  null.json             random-direction null: same entries/exits, mirrored stop;
                        bootstrap of the P&L difference
  score_buckets.csv     label net P&L by score bucket (rejected signals included)
  factor_quintiles.csv  label net R by each factor's quintile
  report.md             all of the above; first line is the survivorship gap
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import polars as pl

from orb.config import Config
from orb.portfolio import TAKEN

PRIMARY = ("default", 1.0)
FACTORS = {"d": "d", "rv": "rv", "r_idx_aligned": "r_idx_aligned", "w": "w", "v": "v"}


# ------------------------------------------------------------------ tables


def _ts(day_col: str, slot_col: str, first: datetime) -> pl.Expr:
    base = pl.col(day_col).cast(pl.Datetime("us")) + pl.duration(
        hours=first.hour, minutes=first.minute
    )
    return base + pl.duration(minutes=pl.col(slot_col))


def trade_log(book: pl.DataFrame, cfg: Config) -> pl.DataFrame:
    first = datetime.combine(date(2000, 1, 1), cfg.session.first_candle)
    t = book.filter(pl.col("book_decision") == TAKEN)
    return t.select(
        "date",
        "symbol",
        "variant",
        "slippage_mult",
        "side",
        "score",
        entry_time=_ts("date", "entry_slot", first),
        entry="entry_price",
        stop="stop",
        qty="qty",
        notional="notional",
        initial_risk="initial_risk",
        exit_time=_ts("date", "exit_slot", first),
        exit="exit_price",
        exit_reason="exit_reason",
        hold_minutes="hold_minutes",
        gross_pnl="gross_pnl",
        costs="costs",
        costs_rounded="costs_rounded",
        slippage="slippage_paid",
        net_pnl="net_pnl",
        net_pnl_rounded="net_pnl_rounded",
        r_gross="r_gross",
        r_net="r_net",
        mfe_r="mfe_r",
        mae_r="mae_r",
        flags="flags",
    ).sort("variant", "slippage_mult", "entry_time", "symbol")


def _stats(df: pl.DataFrame, keys: list[str]) -> pl.DataFrame:
    num = [
        "gross_pnl",
        "costs",
        "net_pnl",
        "net_pnl_rounded",
        "slippage",
        "hold_minutes",
        "r_gross",
        "r_net",
    ]
    df = df.with_columns(pl.col(c).cast(pl.Float64) for c in num if c in df.columns)
    net = pl.col("net_pnl").cast(pl.Float64)
    win = net > 0
    aggs = [
        pl.len().alias("trades"),
        win.mean().round(4).alias("hit_rate"),
        net.filter(win).mean().round(2).alias("avg_win"),
        net.filter(~win).mean().round(2).alias("avg_loss"),
        pl.col("gross_pnl").sum().round(2).alias("gross_pnl"),
        pl.col("costs").sum().round(2).alias("costs"),
        net.sum().round(2).alias("net_pnl"),
        pl.col("net_pnl_rounded").cast(pl.Float64).sum().round(2).alias("net_pnl_rounded"),
        net.mean().round(2).alias("avg_net"),
        pl.col("slippage").sum().round(2).alias("slippage_paid"),
        pl.col("hold_minutes").mean().round(1).alias("avg_hold_min"),
        pl.col("r_gross").mean().round(4).alias("avg_r_gross"),
        pl.col("r_net").mean().round(4).alias("avg_r_net"),
    ]
    out = df.group_by(keys).agg(aggs) if keys else df.select(aggs)
    return out.with_columns(
        payoff=(pl.col("avg_win") / -pl.col("avg_loss")).round(3),
    )


def max_drawdown(daily_net: pl.Series) -> float:
    eq = np.cumsum(daily_net.to_numpy())
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    return float(np.max(peak - eq)) if eq.size else 0.0


def summary(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.height == 0:
        return pl.DataFrame()
    s = _stats(trades, ["variant", "slippage_mult"])
    extra = []
    for (v, m), g in trades.group_by("variant", "slippage_mult"):
        daily = g.group_by("date").agg(pl.col("net_pnl").sum()).sort("date")["net_pnl"]
        losses = g.filter(pl.col("net_pnl") <= 0)["net_pnl"].sum()
        gains = g.filter(pl.col("net_pnl") > 0)["net_pnl"].sum()
        extra.append(
            {
                "variant": v,
                "slippage_mult": m,
                "days_traded": daily.len(),
                "max_drawdown": round(max_drawdown(daily), 2),
                "profit_factor": round(gains / -losses, 3) if losses < 0 else None,
                "stops": g.filter(pl.col("exit_reason") == "STOP").height,
                "hard_exits": g.filter(pl.col("exit_reason") == "HARD_EXIT").height,
                "exits_substituted": g.filter(pl.col("exit_reason") == "EXIT_SUBSTITUTED").height,
            }
        )
    return s.join(pl.DataFrame(extra), on=["variant", "slippage_mult"]).sort(
        "variant", "slippage_mult"
    )


# ----------------------------------------------------------------- regimes


@dataclass
class RegimeTags:
    vix: pl.DataFrame  # date, vix_tercile
    trend: pl.DataFrame  # date, trend_day
    expiry: pl.DataFrame  # date, is_expiry, is_<type>...
    results: pl.DataFrame  # symbol, date (results_day stock-days)


def regime_tables(trades: pl.DataFrame, tags: RegimeTags) -> dict[str, pl.DataFrame]:
    t = trades.join(tags.vix.select("date", "vix_tercile"), on="date", how="left")
    t = t.join(tags.trend.select("date", "trend_day"), on="date", how="left")
    t = t.join(tags.expiry, on="date", how="left")
    t = t.join(
        tags.results.select("symbol", "date").with_columns(results_day=pl.lit(True)),
        on=["symbol", "date"],
        how="left",
    )
    exp_cols = [c for c in tags.expiry.columns if c.startswith("is_")]
    t = t.with_columns(
        pl.col("vix_tercile").fill_null("untagged"),
        *(pl.col(c).fill_null(False) for c in exp_cols),
        day_type=pl.when(pl.col("trend_day").is_null())
        .then(pl.lit("untagged"))
        .when(pl.col("trend_day"))
        .then(pl.lit("trend"))
        .otherwise(pl.lit("range")),
        results_day=pl.col("results_day").fill_null(False),
    )
    out = {
        "vix_tercile": _stats(t, ["vix_tercile"]).sort("vix_tercile"),
        "trend_vs_range": _stats(t, ["day_type"]).sort("day_type"),
        "results_day": _stats(t, ["results_day"]).sort("results_day"),
    }
    for c in exp_cols:
        out[c.removeprefix("is_")] = _stats(t, [c]).sort(c)
    return out


# -------------------------------------------------------------------- null


def _coin_null(a: np.ndarray, b: np.ndarray, draws: int, seed: int) -> dict:
    """Each draw picks, per trade, the actual (a) or the flipped (b) outcome by a
    fair coin. One-sided p = P(random total >= actual total); paired bootstrap
    (trades resampled, coins re-drawn) CI of the mean per-trade difference."""
    rng = np.random.default_rng(seed)
    n = a.size
    totals = np.where(rng.random((draws, n)) < 0.5, a, b).sum(axis=1)
    idx = rng.integers(0, n, (draws, n))
    diffs = (a[idx] - np.where(rng.random((draws, n)) < 0.5, a[idx], b[idx])).mean(axis=1)
    return {
        "usable": int(n),
        "actual_total": round(float(a.sum()), 2),
        "random_mean": round(float(totals.mean()), 2),
        "random_p05": round(float(np.quantile(totals, 0.05)), 2),
        "random_p95": round(float(np.quantile(totals, 0.95)), 2),
        "p_value": round(float((totals >= a.sum()).mean()), 4),
        "mean_diff_per_trade": round(float((a - (a + b) / 2).mean()), 4),
        "mean_diff_ci95": [
            round(float(np.quantile(diffs, 0.025)), 4),
            round(float(np.quantile(diffs, 0.975)), 4),
        ],
    }


def null_test(null: pl.DataFrame, draws: int, seed: int) -> dict:
    """Random-direction null on the primary book's taken trades (decision 55).

    Primary (used for pre-registered criterion c): each trade vs its twin with only
    the direction flipped: same entry candle, same qty, same rupee costs, stop
    mirrored at the same per-share distance, same exit rules. Secondary (reported
    only): strict sign flip at the actual exit time (-gross - costs)."""
    out: dict = {"trades": null.height, "draws": draws, "seed": seed}
    ok = null.filter(pl.col("twin_net_pnl").is_not_null())
    if ok.height == 0:
        return {**out, "usable": 0}
    a = ok["net_pnl"].to_numpy().astype(float)
    out.update(_coin_null(a, ok["twin_net_pnl"].to_numpy().astype(float), draws, seed))
    out["same_exit_time_share"] = round(float(ok["same_exit_time"].mean()), 4)
    out["secondary_sign_flip"] = _coin_null(
        a, ok["flip_net_pnl"].to_numpy().astype(float), draws, seed + 1
    )
    return out


# ----------------------------------------------------------- score validity


def score_validity(
    labels: pl.DataFrame,
    features: pl.DataFrame,
    buckets: list[float],
    quantiles: int,
    variant: str = "default",
) -> dict[str, pl.DataFrame]:
    """Label outcomes of every scored first breakout (taken or not, rejected
    included; EXCLUDED stock-days left out) by score bucket and factor quintile."""
    lb = labels.filter(
        (pl.col("variant") == variant)
        & (pl.col("decision") != "EXCLUDED")
        & pl.col("score").is_not_null()
    )
    f = features.select(
        "date", "symbol", "d", "rv", "w", "v", r_idx_aligned=pl.col("r_idx") * pl.col("side_sign")
    )
    lb = lb.join(f, on=["date", "symbol"], how="left").with_columns(
        pl.col("net_pnl", "r_net", "r_per_share_gross").cast(pl.Float64)
    )
    edges = [-np.inf, *buckets, np.inf]
    names = [f"<{buckets[0]:g}"] + [
        f"{lo:g}-{hi - 1:g}" if np.isfinite(hi) else f"{lo:g}+"
        for lo, hi in zip(edges[1:-1], edges[2:], strict=True)
    ]
    lb = lb.with_columns(score_bucket=pl.col("score").cut(buckets, labels=names, left_closed=True))
    agg = [
        pl.len().alias("signals"),
        pl.col("net_pnl").is_not_null().sum().alias("sized"),
        pl.col("net_pnl").mean().round(2).alias("avg_net_pnl"),
        pl.col("net_pnl").sum().round(2).alias("net_pnl"),
        (pl.col("net_pnl") > 0).mean().round(4).alias("hit_rate"),
        pl.col("r_net").mean().round(4).alias("avg_r_net"),
        pl.col("r_per_share_gross").mean().round(4).alias("avg_r_per_share_gross"),
    ]
    out = {"score_buckets": lb.group_by("score_bucket").agg(agg).sort("score_bucket")}
    rows = []
    for name, col in FACTORS.items():
        g = lb.filter(pl.col(col).is_not_null())
        if g.height < quantiles:
            continue
        q = g.with_columns(
            quintile=(pl.col(col).rank("ordinal") * quantiles / (g.height + 1))
            .floor()
            .cast(pl.Int64)
            + 1
        )
        t = (
            q.group_by("quintile")
            .agg(pl.col(col).min().alias("lo"), pl.col(col).max().alias("hi"), *agg)
            .with_columns(factor=pl.lit(name))
        )
        rows.append(t)
    out["factor_quintiles"] = pl.concat(rows).sort("factor", "quintile") if rows else pl.DataFrame()
    return out


# --------------------------------------------------------- pre-registration


@dataclass
class Criterion:
    key: str
    text: str
    value: str
    passed: bool

    def line(self) -> str:
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.key}. {self.text}: {self.value}"


def criteria(
    trades: pl.DataFrame, null_result: dict, labels: pl.DataFrame, cfg: Config, oos: bool
) -> tuple[list[Criterion], str]:
    """docs/PREREGISTRATION.md criteria on the primary entry-candle variant.

    a. >= min_trades taken trades (1x)                     [in-sample gate only]
    b. net (rupee-rounded) > 0 at 1x and >= 0 at 2x slippage
    c. random-direction null (primary twin) p < null_p_max
    d. net (rounded, 1x) > 0 in >= min_positive_year_share of calendar years traded
    e. score useful: label net R per trade strictly rises across 65-74, 75-84, 85+
       (diagnostic: if not, V1 is evaluated as plain ORB)
    """
    pr = cfg.prereg
    var = PRIMARY[0]

    def book(mult: float) -> pl.DataFrame:
        if trades.height == 0:
            return trades
        return trades.filter((pl.col("variant") == var) & (pl.col("slippage_mult") == mult))

    base, stress = book(pr.base_slippage), book(pr.stress_slippage)

    def net(df: pl.DataFrame) -> float:
        return float(df["net_pnl_rounded"].cast(pl.Float64).sum()) if df.height else 0.0

    out = []
    n = base.height
    out.append(Criterion("a", f">= {pr.min_trades} taken trades", str(n), n >= pr.min_trades))
    nb, ns = net(base), net(stress)
    out.append(
        Criterion(
            "b",
            f"net P&L (rupee-rounded) > 0 at {pr.base_slippage:g}x and >= 0 at "
            f"{pr.stress_slippage:g}x slippage",
            f"{nb:.2f} / {ns:.2f}",
            n > 0 and nb > 0 and stress.height > 0 and ns >= 0,
        )
    )
    p = null_result.get("p_value")
    out.append(
        Criterion(
            "c",
            f"beats random-direction null at p < {pr.null_p_max:g}",
            "n/a" if p is None else f"p = {p:.4f}",
            p is not None and p < pr.null_p_max,
        )
    )
    if n:
        yearly = base.group_by(pl.col("date").dt.year().alias("y")).agg(
            pl.col("net_pnl_rounded").cast(pl.Float64).sum().alias("net")
        )
        pos, yrs = int((yearly["net"] > 0).sum()), yearly.height
    else:
        pos, yrs = 0, 0
    share = pos / yrs if yrs else 0.0
    out.append(
        Criterion(
            "d",
            f"net positive in >= {pr.min_positive_year_share:.0%} of calendar years",
            f"{pos}/{yrs} ({share:.0%})",
            yrs > 0 and share >= pr.min_positive_year_share,
        )
    )
    edges = pr.score_buckets
    lb = (
        labels.filter(
            (pl.col("variant") == var)
            & (pl.col("decision") != "EXCLUDED")
            & (pl.col("score") >= edges[0])
        )
        if labels.height
        else labels
    )
    means = []
    for lo, hi in zip(edges, [*edges[1:], float("inf")], strict=True):
        g = (
            lb.filter((pl.col("score") >= lo) & (pl.col("score") < hi))["r_net"]
            if lb.height
            else pl.Series([], dtype=pl.Float64)
        )
        g = g.cast(pl.Float64).drop_nulls()
        means.append(float(g.mean()) if g.len() else None)
    rising = all(m is not None for m in means) and all(
        b - a > 1e-9 for a, b in zip(means, means[1:], strict=False)
    )
    shown = " < ".join("n/a" if m is None else f"{m:.3f}" for m in means)
    out.append(
        Criterion(
            "e",
            "score useful: net R per trade rises across 65-74, 75-84, 85+ (else V1 = plain ORB)",
            shown,
            rising,
        )
    )
    gate = [c for c in out if c.key in ("b", "c", "d")] + ([] if oos else [out[0]])
    ok = all(c.passed for c in gate)
    if oos:
        verdict = f"OOS VERDICT ({pr.version}): {'PASS' if ok else 'FAIL'} on b-d"
    else:
        verdict = (
            f"IN-SAMPLE GATE ({pr.version}, a-d): {'PASS' if ok else 'FAIL'}"
            f" -> OOS run {'allowed' if ok else 'NOT allowed'}"
        )
    if not rising:
        verdict += "; score not shown useful -> evaluate V1 as plain ORB"
    return out, verdict


# ------------------------------------------------------------------ report


def _md(df: pl.DataFrame) -> str:
    if df.height == 0:
        return "_(none)_\n"
    cols = df.columns
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in df.iter_rows():
        lines.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    return "\n".join(lines) + "\n"


def build_report(run_dir: str | Path, cfg: Config, tags: RegimeTags) -> Path:
    run = Path(run_dir)
    meta = json.loads((run / "meta.json").read_text())
    book = pl.read_parquet(run / "book.parquet")
    labels = pl.read_parquet(run / "labels.parquet")
    features = pl.read_parquet(run / "features.parquet")
    null = (
        pl.read_parquet(run / "null.parquet")
        if (run / "null.parquet").exists()
        else (pl.DataFrame())
    )
    out = run / "report"
    out.mkdir(exist_ok=True)

    trades = trade_log(book, cfg) if book.height else pl.DataFrame()
    trades.write_csv(out / "trade_log.csv")
    if (run / "signal_log.csv").exists():
        (out / "signal_log.csv").write_bytes((run / "signal_log.csv").read_bytes())
    summ = summary(trades) if trades.height else pl.DataFrame()
    summ.write_csv(out / "summary.csv")
    prim = (
        trades.filter((pl.col("variant") == PRIMARY[0]) & (pl.col("slippage_mult") == PRIMARY[1]))
        if trades.height
        else trades
    )
    regimes = regime_tables(prim, tags) if prim.height else {}
    for k, v in regimes.items():
        v.write_csv(out / f"regime_{k}.csv")
    nt = null_test(null, cfg.validation.null_bootstrap, cfg.run.seed) if null.height else {}
    (out / "null.json").write_text(json.dumps(nt, indent=1))
    sv = (
        score_validity(
            labels, features, cfg.validation.score_buckets, cfg.validation.factor_quantiles
        )
        if labels.height
        else {}
    )
    for k, v in sv.items():
        v.write_csv(out / f"{k}.csv")

    crit, verdict = criteria(trades, nt, labels, cfg, bool(meta.get("oos")))
    (out / "criteria.json").write_text(
        json.dumps({"verdict": verdict, "criteria": [c.__dict__ for c in crit]}, indent=1)
    )
    git = meta.get("git", {})
    sample = "OUT-OF-SAMPLE" if meta.get("oos") else "in-sample"
    lines = [
        meta.get("survivorship_gap", "survivorship gap: n/a"),
        *[c.line() for c in crit],
        verdict,
        *(
            []
            if meta.get("prereg_match", True)
            else ["WARNING: config differs from docs/PREREGISTRATION.md: not a pre-registered run"]
        ),
        "",
        f"# ORB backtest report: run {meta.get('run_id')}",
        "",
        f"- period: {meta.get('start')} .. {meta.get('end')} ({sample})",
        f"- config hash: `{meta.get('config_hash')}`",
        f"- git: `{git.get('commit')}`{' (dirty)' if git.get('dirty') else ''}",
        f"- data version: `{meta.get('data_version')}` "
        f"(vendor snapshot {meta.get('vendor_snapshot_id')})",
        "- primary book: default entry-candle rule, 1x slippage; gross and net shown separately",
        "",
        "## Summary (all books)",
        _md(summ),
        "## Regimes (primary book)",
    ]
    for k, v in regimes.items():
        lines += [f"### {k}", _md(v)]
    primary_null = {k: v for k, v in nt.items() if k != "secondary_sign_flip"}
    lines += [
        "## Random-direction null (primary book): criterion c",
        "```",
        json.dumps(primary_null, indent=1),
        "```",
        "### Secondary diagnostic: strict sign flip at the actual exit time",
        "_Excluded from pass/fail (pre-registration amendment 2026-10-02)._",
        "```",
        json.dumps(nt.get("secondary_sign_flip", {}), indent=1),
        "```",
        "",
    ]
    for k, v in sv.items():
        lines += [f"## Score validity: {k}", _md(v)]
    (out / "report.md").write_text("\n".join(lines))
    return out


def tags_from_disk(cfg: Config) -> RegimeTags:
    from orb.data.pipeline import Stores
    from orb.data.reference import load_reference
    from orb.regimes import expiry_flags, results_days, trend_days, vix_terciles

    raw = Stores.raw_only(cfg.data.root)
    ref = load_reference(cfg.reference)
    start, end = cfg.data.daily_history_start, cfg.run.end_date
    idx = raw.read_daily(cfg.data.index.primary, start, end)
    vix = raw.read_daily(cfg.data.index.vix, start, end)
    cal = idx["date"].to_list()
    return RegimeTags(
        vix=vix_terciles(vix, cfg.validation.vix_min_history)
        if vix.height
        else pl.DataFrame(schema={"date": pl.Date, "vix_tercile": pl.String}),
        trend=trend_days(idx, cfg.validation.trend_day_threshold),
        expiry=expiry_flags(ref.expiries),
        results=results_days(ref.results_dates, cal),
    )
