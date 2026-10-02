"""V2 report (``orb report --strategy v2 <run>``), per docs/PREREGISTRATION_V2.md.

report.md, top to bottom:
  survivorship gap; [PASS]/[FAIL] lines a-e and the gate verdict; capital model
  and ruin dates; headline metrics per book; skip / exit-reason counts; nulls
  (primary random direction, always-long, index variant); secondary diagnostics
  (drift excluded, post-2020, results days, long vs short, regime tables);
  criterion e by |z| tercile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from orb import csvio
from orb.reports import RegimeTags, _coin_null, _md, max_drawdown, regime_tables
from orb.v2.config import ConfigV2
from orb.v2.engine import INDEX_BOOK, TAKEN

SKIP_REASONS = ["LOCKED_CIRCUIT", "ENTRY_MISSING", "QTY_ZERO", "NO_TICK", "RUIN"]
EXIT_REASONS = ["HARD_EXIT", "EXIT_SUBSTITUTED", "EXIT_LOCKED", "EXIT_LOCKED_UNFILLED"]


@dataclass
class Criterion:
    key: str
    text: str
    value: str
    passed: bool

    def line(self) -> str:
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.key}. {self.text}: {self.value}"


# ------------------------------------------------------------------ tables


def taken(book: pl.DataFrame, name: str | None = None) -> pl.DataFrame:
    if book.height == 0 or "net_pnl" not in book.columns:
        return pl.DataFrame()
    t = book.filter(pl.col("book_decision") == TAKEN)
    return t.filter(pl.col("book") == name) if name else t


def as_v1_columns(t: pl.DataFrame) -> pl.DataFrame:
    """Columns V1's regime/stat helpers expect: R becomes return on notional."""
    return t.with_columns(
        slippage=pl.col("slippage_paid"),
        r_gross=pl.col("gross_pnl") / pl.col("notional"),
        r_net=pl.col("net_pnl") / pl.col("notional"),
    )


def _ret_names(df: pl.DataFrame) -> pl.DataFrame:
    return df.rename(
        {c: c.replace("avg_r_", "avg_ret_") for c in df.columns if c.startswith("avg_r_")}
    )


def ruin_dates(meta: dict) -> dict:
    return meta.get("ruin") or {}


def headline(book: pl.DataFrame, equity: pl.DataFrame, meta: dict, cfg: ConfigV2) -> pl.DataFrame:
    rows = []
    names = list(cfg.execution.books) + [INDEX_BOOK]
    for name in names:
        t = taken(book, name)
        e = equity.filter(pl.col("book") == name).sort("date") if equity.height else equity
        net = t["net_pnl"] if t.height else pl.Series([], dtype=pl.Float64)
        daily = (
            t.group_by("date").agg(pl.col("net_pnl_rounded").sum()).sort("date")["net_pnl_rounded"]
            if t.height
            else pl.Series([], dtype=pl.Float64)
        )
        rows.append(
            {
                "book": name,
                "trades": t.height,
                "hit_rate": round(float((net > 0).mean()), 4) if t.height else None,
                "gross_pnl": round(float(t["gross_pnl"].sum()), 2) if t.height else 0.0,
                "costs": round(float(t["costs"].sum()), 2) if t.height else 0.0,
                "costs_rounded": round(float(t["costs_rounded"].sum()), 2) if t.height else 0.0,
                "net_pnl": round(float(net.sum()), 2) if t.height else 0.0,
                "net_pnl_rounded": round(float(t["net_pnl_rounded"].sum()), 2) if t.height else 0.0,
                "slippage_paid": round(float(t["slippage_paid"].sum()), 2) if t.height else 0.0,
                "avg_ret_net": round(float((t["net_pnl"] / t["notional"]).mean()), 6)
                if t.height
                else None,
                "max_drawdown": round(max_drawdown(daily), 2),
                "final_equity": round(float(e["equity_end"][-1]), 2) if e.height else None,
                "ruin_date": ruin_dates(meta).get(name),
                "days_traded": t["date"].n_unique() if t.height else 0,
                **{
                    r.lower(): int((t["exit_reason"].cast(pl.String) == r).sum()) if t.height else 0
                    for r in EXIT_REASONS
                },
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None)


def skip_counts(book: pl.DataFrame, signals: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for name, g in book.group_by("book") if book.height else []:
        name = name[0]
        rows.append(
            {
                "book": name,
                "selected": g.height,
                **{
                    r.lower(): int((g["book_reason"].cast(pl.String) == r).sum())
                    for r in SKIP_REASONS
                },
            }
        )
    out = pl.DataFrame(rows).sort("book") if rows else pl.DataFrame()
    p0945 = int(signals["p0945_substituted"].fill_null(False).sum()) if signals.height else 0
    return out.with_columns(p0945_substituted_signals=pl.lit(p0945)) if out.height else out


# -------------------------------------------------------------------- nulls


def null_primary(t: pl.DataFrame, draws: int, seed: int) -> dict:
    """Random direction per trade (only the raw move flips; slippage and the actual
    rupee costs stay costs); actual statistic on the same unrounded costs."""
    if t.height == 0:
        return {"trades": 0, "usable": 0}
    raw = t["raw_move"].to_numpy().astype(float)
    cost = (t["slippage_paid"] + t["costs"]).to_numpy().astype(float)
    out = {"trades": t.height, "draws": draws, "seed": seed}
    out.update(_coin_null(raw - cost, -raw - cost, draws, seed))
    return out


def always_long(t: pl.DataFrame) -> dict:
    if t.height == 0:
        return {"trades": 0}
    long_raw = (
        pl.when(pl.col("side") == "long").then(pl.col("raw_move")).otherwise(-pl.col("raw_move"))
    )
    x = t.with_columns(long_net=long_raw - pl.col("slippage_paid") - pl.col("costs"))
    return {
        "trades": t.height,
        "net_actual_unrounded": round(float(t["net_pnl"].sum()), 2),
        "net_always_long_unrounded": round(float(x["long_net"].sum()), 2),
        "hit_rate_always_long": round(float((x["long_net"] > 0).mean()), 4),
    }


# ---------------------------------------------------------------- criteria


def z_terciles(labels: pl.DataFrame, n: int) -> pl.DataFrame:
    lb = labels.filter(pl.col("status") == "OK") if labels.height else labels
    if lb.height < n:
        return pl.DataFrame()
    q = lb.with_columns(
        tercile=(pl.col("abs_z").rank("ordinal") * n / (lb.height + 1)).floor().cast(pl.Int64) + 1
    )
    return (
        q.group_by("tercile")
        .agg(
            lo=pl.col("abs_z").min(),
            hi=pl.col("abs_z").max(),
            labels=pl.len(),
            avg_ret_net=pl.col("ret_net").mean(),
            avg_ret_gross=pl.col("ret_gross").mean(),
            avg_net_pnl=pl.col("net_pnl").mean(),
            hit_rate=(pl.col("net_pnl") > 0).mean(),
        )
        .sort("tercile")
    )


def criteria(
    book: pl.DataFrame, nt: dict, labels: pl.DataFrame, meta: dict, cfg: ConfigV2, oos: bool
) -> tuple[list[Criterion], str]:
    pr = cfg.prereg
    pb, sb = cfg.execution.primary_book, cfg.execution.stress_book
    base, stress = taken(book, pb), taken(book, sb)

    def net(t: pl.DataFrame) -> float:
        return float(t["net_pnl_rounded"].sum()) if t.height else 0.0

    out = []
    n = base.height
    out.append(Criterion("a", f">= {pr.min_trades} taken trades", str(n), n >= pr.min_trades))
    nb, ns = net(base), net(stress)
    out.append(
        Criterion(
            "b",
            f"net P&L (rupee-rounded) > 0 under {pb} slippage and >= 0 under {sb} stress",
            f"{nb:.2f} / {ns:.2f}",
            n > 0 and nb > 0 and stress.height > 0 and ns >= 0,
        )
    )
    p = nt.get("p_value")
    out.append(
        Criterion(
            "c",
            f"beats random-direction null at p < {pr.null_p_max:g} "
            f"(Bonferroni, {pr.strategies_tested} strategies)",
            "n/a" if p is None else f"p = {p:.4f}",
            p is not None and p < pr.null_p_max,
        )
    )
    if n:
        yearly = base.group_by(pl.col("date").dt.year().alias("y")).agg(
            pl.col("net_pnl_rounded").sum().alias("net")
        )
        pos, yrs = int((yearly["net"] > 0).sum()), yearly.height
    else:
        pos, yrs = 0, 0
    share = pos / yrs if yrs else 0.0
    ruin = ruin_dates(meta).get(pb)
    out.append(
        Criterion(
            "d",
            f"net positive in >= {pr.min_positive_year_share:.0%} of calendar years "
            "and the ruin stop never hit",
            f"{pos}/{yrs} ({share:.0%}); ruin {ruin or 'never'}",
            yrs > 0 and share >= pr.min_positive_year_share and ruin is None,
        )
    )
    zt = z_terciles(labels, cfg.validation.z_terciles)
    means = zt["avg_ret_net"].to_list() if zt.height else []
    rising = len(means) == cfg.validation.z_terciles and all(
        b - a > 1e-12 for a, b in zip(means, means[1:], strict=False)
    )
    shown = " < ".join(f"{m:.5f}" for m in means) if means else "n/a"
    out.append(
        Criterion(
            "e",
            "(informational) net return per trade rises across |z| terciles of qualifying stocks",
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
    return out, verdict


# --------------------------------------------------- secondary diagnostics


def secondary(
    book: pl.DataFrame,
    drift_days: pl.DataFrame,
    tags: RegimeTags,
    cfg: ConfigV2,
) -> pl.DataFrame:
    """Subsets of the booked trades (equity path NOT re-run; qty stay as booked)."""
    pb, sb = cfg.execution.primary_book, cfg.execution.stress_book
    drift = drift_days.select("symbol", "date").unique()
    results = tags.results.select("symbol", "date").unique()

    def scenario(name, keep):
        b, s = keep(taken(book, pb)), keep(taken(book, sb))
        nt = null_primary(b, cfg.validation.null_bootstrap, cfg.run.seed)
        return {
            "scenario": name,
            "trades": b.height,
            f"net_rounded_{pb}": round(float(b["net_pnl_rounded"].sum()), 2) if b.height else 0.0,
            f"net_rounded_{sb}": round(float(s["net_pnl_rounded"].sum()), 2) if s.height else 0.0,
            "null_p": nt.get("p_value"),
        }

    def anti(df, other):
        return df.join(other, on=["symbol", "date"], how="anti") if df.height else df

    return pl.DataFrame(
        [
            scenario("all trades (reference = criteria a-c)", lambda d: d),
            scenario("drift stock-days excluded", lambda d: anti(d, drift)),
            scenario(
                f"from {cfg.validation.post_start} (lower survivorship gap)",
                lambda d: d.filter(pl.col("date") >= cfg.validation.post_start) if d.height else d,
            ),
            scenario("results-day trades removed", lambda d: anti(d, results)),
        ]
    )


def results_share(book: pl.DataFrame, tags: RegimeTags, cfg: ConfigV2) -> str:
    t = taken(book, cfg.execution.primary_book)
    if t.height == 0:
        return "results days: no trades"
    on = t.join(tags.results.select("symbol", "date").unique(), on=["symbol", "date"]).height
    return f"share of primary-book trades on results days: {on}/{t.height} ({on / t.height:.1%})"


# ------------------------------------------------------------------ report


def build_report_v2(
    run_dir: str | Path, cfg: ConfigV2, tags: RegimeTags, drift_days: pl.DataFrame | None = None
) -> Path:
    run = Path(run_dir)
    meta = json.loads((run / "meta.json").read_text())
    book = pl.read_parquet(run / "book.parquet")
    equity = pl.read_parquet(run / "equity.parquet")
    labels = pl.read_parquet(run / "labels.parquet")
    signals = pl.read_parquet(run / "signals.parquet")
    out = run / "report"
    out.mkdir(exist_ok=True)
    drift_days = (
        drift_days
        if drift_days is not None
        else pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date})
    )
    pb = cfg.execution.primary_book
    prim = taken(book, pb)
    tl = taken(book)
    csvio.write_csv(
        tl.drop([c for c in tl.columns if tl[c].dtype == pl.Null]), out / "trade_log.csv"
    )
    if (run / "signal_log.csv").exists():
        (out / "signal_log.csv").write_bytes((run / "signal_log.csv").read_bytes())
    nt = null_primary(prim, cfg.validation.null_bootstrap, cfg.run.seed)
    (out / "null.json").write_text(json.dumps(nt, indent=1))
    crit, verdict = criteria(book, nt, labels, meta, cfg, bool(meta.get("oos")))
    (out / "criteria.json").write_text(
        json.dumps({"verdict": verdict, "criteria": [c.__dict__ for c in crit]}, indent=1)
    )
    head = headline(book, equity, meta, cfg)
    csvio.write_csv(head, out / "summary.csv")
    skips = skip_counts(book, signals)
    csvio.write_csv(skips, out / "skips.csv")
    al = always_long(prim)
    idx_t = taken(book, INDEX_BOOK)
    idx = {
        "trades": idx_t.height,
        "net_rounded": round(float(idx_t["net_pnl_rounded"].sum()), 2) if idx_t.height else 0.0,
        "ruin_date": ruin_dates(meta).get(INDEX_BOOK),
        "null": null_primary(idx_t, cfg.validation.null_bootstrap, cfg.run.seed),
    }
    sd = secondary(book, drift_days, tags, cfg)
    csvio.write_csv(sd, out / "secondary_diagnostics.csv")
    regimes = {
        k: _ret_names(v)
        for k, v in (regime_tables(as_v1_columns(prim), tags) if prim.height else {}).items()
    }
    for k, v in regimes.items():
        csvio.write_csv(v, out / f"regime_{k}.csv")
    zt = z_terciles(labels, cfg.validation.z_terciles)
    csvio.write_csv(zt, out / "z_terciles.csv")
    prov = meta.get("provenance") or {}
    if prov.get("pinned_by_this_run"):
        prov_line = (
            f"- code: commit `{prov['commit']}`; **this run pinned it** (first V2 in-sample run)"
        )
    elif prov.get("pinned_commit"):
        d = prov.get("diff_since_pin") or {}
        prov_line = (
            f"- code: pinned V2 commit `{prov['pinned_commit']}`; this run `{prov.get('commit')}`; "
            f"result-relevant files changed since the pin: "
            f"{', '.join(d.get('relevant_files', [])) or 'none'}"
        )
    else:
        prov_line = "- code: no provenance recorded"
    ruin = ruin_dates(meta)
    lines = [
        meta.get("survivorship_gap", "survivorship gap: n/a"),
        *[c.line() for c in crit],
        verdict,
        *(
            []
            if meta.get("prereg_match", True)
            else [
                "WARNING: config differs from docs/PREREGISTRATION_V2.md: not a pre-registered run"
            ]
        ),
        f"capital model: equity tracked per book from Rs {cfg.sizing.starting_capital:,.0f}; "
        f"Slot_d = {cfg.sizing.deploy_fraction:.0%} x equity_d / {cfg.selection.max_positions}; "
        f"no new trades once equity_d < Rs {cfg.sizing.ruin_equity:,.0f}",
        "ruin dates: "
        + ", ".join(f"{k} {ruin.get(k) or 'never'}" for k in [*cfg.execution.books, INDEX_BOOK]),
        "",
        f"# V2 backtest report: run {meta.get('run_id')}",
        "",
        f"- period: {meta.get('start')} .. {meta.get('end')} "
        f"({'OUT-OF-SAMPLE' if meta.get('oos') else 'in-sample'})",
        f"- config hash: `{meta.get('config_hash')}`",
        f"- data version: `{meta.get('data_version')}` "
        f"(vendor snapshot {meta.get('vendor_snapshot_id')})",
        f"- primary book: `{pb}` slippage, rupee-rounded costs; "
        f"stress: `{cfg.execution.stress_book}`",
        prov_line,
        "",
        "## Headline metrics (every book)",
        _md(head),
        "## Skips and flags",
        _md(skips),
        "## Nulls",
        "### Primary: random direction per trade (criterion c)",
        "_Only the raw move flips; slippage and the actual unrounded costs stay costs "
        "on both sides._",
        "```",
        json.dumps(nt, indent=1),
        "```",
        "### Secondary: always-long baseline (same trades, qty and costs)",
        "```",
        json.dumps(al, indent=1),
        "```",
        "### Secondary: index-level variant (own equity path)",
        "```",
        json.dumps(idx, indent=1, default=str),
        "```",
        "",
        "## Secondary diagnostics (excluded from pass/fail)",
        "_Trades removed from the existing books; the equity path is not re-run._",
        _md(sd),
        results_share(book, tags, cfg),
        "",
    ]
    for k, v in regimes.items():
        lines += [f"### {k}", _md(v)]
    lines += ["## Criterion e: label net return by |z| tercile (informational)", _md(zt)]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    return out
