"""V3 report (``orb report --strategy v3 <run>``), per docs/PREREGISTRATION_V3.md.

report.md, top to bottom: survivorship gap; [PASS]/[FAIL] lines a-e and the gate
verdict; capital model, ruin dates, pre-tax and beta notes; metrics per book;
nulls (random selection, per-year comparison); secondary baselines; secondary
diagnostics; regime tables; flag counts; criterion e by r_week quintile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

from orb import csvio
from orb.reports import RegimeTags, _md
from orb.v3.config import ConfigV3

FLAG_KEYS = [
    "exit:EXIT_DEFERRED",
    "exit:EXIT_CARRIED",
    "exit:EXIT_CORP_ACTION",
    "exit:EXIT_DELISTED",
    "exit:EXIT_NO_MINUTE_DATA",
    "exit:EXIT_LOCKED",
    "CASH_LIMITED",
    "QTY_ZERO",
    "LOCKED_CIRCUIT",
    "ENTRY_MISSING",
    "exit:FORCED_END",
    "DIVIDEND_NO_AMOUNT",
    "FRACTION_DROPPED",
    "RIGHTS_HELD_THROUGH",
    "CORP_ACTION_NEXT_DAY",
    "RUIN",
    "NO_TICK",
]


@dataclass
class Criterion:
    key: str
    text: str
    value: str
    passed: bool

    def line(self) -> str:
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.key}. {self.text}: {self.value}"


def _years(year_end: dict, capital: float) -> dict[int, float]:
    """Annual net from year-end equity (keys may be str after JSON)."""
    ye = {int(k): float(v) for k, v in year_end.items()}
    out, prev = {}, capital
    for y in sorted(ye):
        out[y] = ye[y] - prev
        prev = ye[y]
    return out


def null_summary(actual_final: float, finals: list[float]) -> dict:
    f = np.asarray(finals, dtype=float)
    if f.size == 0:
        return {"draws": 0}
    return {
        "draws": int(f.size),
        "actual_final_equity": round(actual_final, 2),
        "null_mean": round(float(f.mean()), 2),
        "null_p05": round(float(np.quantile(f, 0.05)), 2),
        "null_p50": round(float(np.quantile(f, 0.5)), 2),
        "null_p95": round(float(np.quantile(f, 0.95)), 2),
        "p_value": round(float((f >= actual_final).mean()), 4),
    }


def per_year(actual_ye: dict, null_ye: list[dict], capital: float) -> pl.DataFrame:
    act = _years(actual_ye, capital)
    nulls = [_years(y, capital) for y in null_ye]
    rows = []
    for y, a in act.items():
        vals = [n[y] for n in nulls if y in n]
        m = float(np.mean(vals)) if vals else None
        rows.append(
            {
                "year": y,
                "actual_net": round(a, 2),
                "null_mean_net": None if m is None else round(m, 2),
                "beats_null": m is not None and a > m,
            }
        )
    return pl.DataFrame(rows)


def quintiles(labels: pl.DataFrame, n: int) -> pl.DataFrame:
    lb = labels.filter(pl.col("ret_gross").is_not_null()) if labels.height else labels
    if lb.height == 0:
        return pl.DataFrame()
    q = lb.with_columns(
        _r=pl.col("r_week").rank("ordinal").over("date"), _n=pl.len().over("date")
    ).with_columns(quintile=((pl.col("_r") - 1) * n / pl.col("_n")).floor().cast(pl.Int64) + 1)
    return (
        q.group_by("quintile")
        .agg(
            labels=pl.len(),
            r_week_lo=pl.col("r_week").min(),
            r_week_hi=pl.col("r_week").max(),
            avg_ret_gross=pl.col("ret_gross").mean(),
            avg_ret_net=pl.col("ret_net").mean(),
            hit_rate_gross=(pl.col("ret_gross") > 0).mean(),
        )
        .sort("quintile", descending=True)  # top r_week quintile first
    )


def book_metrics(
    trades: pl.DataFrame, daily: pl.DataFrame, meta: dict, cfg: ConfigV3
) -> pl.DataFrame:
    rows = []
    for name in cfg.execution.books:
        t = trades.filter(pl.col("book") == name) if trades.height else trades
        dly = daily.filter(pl.col("book") == name).sort("date") if daily.height else daily
        b = meta["books"].get(name, {})
        eq = dly["equity"].to_numpy() if dly.height else np.array([cfg.sizing.starting_capital])
        peak = np.maximum.accumulate(np.concatenate([[cfg.sizing.starting_capital], eq]))[1:]
        rows.append(
            {
                "book": name,
                "round_trips": t.height,
                "hit_rate": round(float((t["net_pnl_rounded"] > 0).mean()), 4)
                if t.height
                else None,
                "gross_pnl": round(float(t["gross_pnl"].sum()), 2) if t.height else 0.0,
                "dividends": round(float(t["dividends"].sum()), 2) if t.height else 0.0,
                "costs": round(float(t["costs"].sum()), 2) if t.height else 0.0,
                "costs_rounded": round(float(t["costs_rounded"].sum()), 2) if t.height else 0.0,
                "dp": round(float(t["dp"].sum()), 2) if t.height else 0.0,
                "slippage_paid": round(float(t["slippage_paid"].sum()), 2) if t.height else 0.0,
                "net_pnl": round(float(t["net_pnl"].sum()), 2) if t.height else 0.0,
                "net_pnl_rounded": round(float(t["net_pnl_rounded"].sum()), 2) if t.height else 0.0,
                "final_equity": round(float(b.get("final_equity", 0.0)), 2),
                "max_drawdown": round(float(np.max(peak - eq)), 2) if eq.size else 0.0,
                "ruin_date": b.get("ruin_date"),
                "avg_holding_days": round(float(t["holding_days"].mean()), 2) if t.height else None,
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None)


def criteria(
    meta: dict,
    trades: pl.DataFrame,
    finals: list,
    null_ye: list,
    labels: pl.DataFrame,
    cfg: ConfigV3,
    oos: bool,
) -> tuple[list[Criterion], str, dict, pl.DataFrame, pl.DataFrame]:
    pr, cap = cfg.prereg, cfg.sizing.starting_capital
    pb, sb = cfg.execution.primary_book, cfg.execution.stress_book
    base = trades.filter(pl.col("book") == pb) if trades.height else trades
    stress = trades.filter(pl.col("book") == sb) if trades.height else trades

    def net(t):
        return float(t["net_pnl_rounded"].sum()) if t.height else 0.0

    out = []
    n = base.height
    out.append(
        Criterion(
            "a", f">= {pr.min_round_trips} completed round trips", str(n), n >= pr.min_round_trips
        )
    )
    nb, ns = net(base), net(stress)
    out.append(
        Criterion(
            "b",
            f"net P&L (rupee-rounded) > 0 under {pb} and >= 0 under {sb} stress",
            f"{nb:.2f} / {ns:.2f}",
            n > 0 and nb > 0 and stress.height > 0 and ns >= 0,
        )
    )
    actual_final = float(meta["books"][pb]["final_equity"])
    ns_ = null_summary(actual_final, finals)
    p = ns_.get("p_value")
    out.append(
        Criterion(
            "c",
            f"beats the random-selection null at p < {pr.null_p_max:.4f} "
            f"(final equity; Bonferroni, {pr.strategies_tested} strategies)",
            "n/a"
            if p is None
            else f"p = {p:.4f} (actual {actual_final:.2f} vs null mean {ns_['null_mean']:.2f})",
            p is not None and p < pr.null_p_max,
        )
    )
    py = per_year(meta["books"][pb]["year_end"], null_ye, cap)
    beat, yrs = (int(py["beats_null"].sum()), py.height) if py.height else (0, 0)
    share = beat / yrs if yrs else 0.0
    ruin = meta["books"][pb].get("ruin_date")
    out.append(
        Criterion(
            "d",
            f"beats the null's mean annual net in >= {pr.min_years_beating_null_share:.0%} "
            "of calendar years and the ruin stop never hit",
            f"{beat}/{yrs} ({share:.0%}); ruin {ruin or 'never'}",
            yrs > 0 and share >= pr.min_years_beating_null_share and ruin is None,
        )
    )
    qt = quintiles(labels, cfg.validation.label_quantiles)
    means = qt["avg_ret_gross"].to_list() if qt.height else []
    rising = len(means) == cfg.validation.label_quantiles and all(
        b - a > 1e-12 for a, b in zip(means, means[1:], strict=False)
    )
    shown = " < ".join(f"{m:.5f}" for m in means) if means else "n/a"
    out.append(
        Criterion(
            "e",
            "(informational) avg next-week GROSS label return rises from the top to the bottom "
            "r_week quintile",
            shown,
            rising,
        )
    )
    gate = [c for c in out if c.key in ("b", "c", "d")] + ([] if oos else [out[0]])
    ok = all(c.passed for c in gate)
    verdict = (
        f"OOS VERDICT ({pr.version}): {'PASS' if ok else 'FAIL'} on b-d"
        if oos
        else f"IN-SAMPLE GATE ({pr.version}, a-d): {'PASS' if ok else 'FAIL'} -> OOS run "
        f"{'allowed' if ok else 'NOT allowed'}"
    )
    return out, verdict, ns_, py, qt


def _stats(t: pl.DataFrame, key: str) -> pl.DataFrame:
    if t.height == 0:
        return pl.DataFrame()
    return (
        t.group_by(key)
        .agg(
            round_trips=pl.len(),
            hit_rate=(pl.col("net_pnl_rounded") > 0).mean().round(4),
            gross_pnl=pl.col("gross_pnl").sum().round(2),
            net_pnl_rounded=pl.col("net_pnl_rounded").sum().round(2),
            avg_net=pl.col("net_pnl_rounded").mean().round(2),
            avg_ret_net=(pl.col("net_pnl_rounded") / pl.col("notional")).mean().round(5),
        )
        .sort(key)
    )


def regimes(t: pl.DataFrame, tags: RegimeTags) -> dict[str, pl.DataFrame]:
    if t.height == 0:
        return {}
    wk = pl.struct(y=pl.col("entry_date").dt.iso_year(), w=pl.col("entry_date").dt.week())
    exp_dates = (
        tags.expiry.filter(pl.col("is_expiry"))
        if "is_expiry" in tags.expiry.columns
        else tags.expiry
    )
    exp_weeks = {(d.isocalendar()[0], d.isocalendar()[1]) for d in exp_dates["date"].to_list()}
    bud_weeks = {(d.isocalendar()[0], d.isocalendar()[1]) for d in tags.budget["date"].to_list()}
    x = t.join(
        tags.vix.select(pl.col("date").alias("entry_date"), "vix_tercile"),
        on="entry_date",
        how="left",
    )
    x = x.with_columns(
        pl.col("vix_tercile").fill_null("untagged"),
        expiry_week=wk.map_elements(
            lambda r: (r["y"], r["w"]) in exp_weeks, return_dtype=pl.Boolean
        ),
        budget_week=wk.map_elements(
            lambda r: (r["y"], r["w"]) in bud_weeks, return_dtype=pl.Boolean
        ),
    )
    return {k: _stats(x, k) for k in ("vix_tercile", "expiry_week", "budget_week")}


def secondary(t: pl.DataFrame, drift: pl.DataFrame, tags: RegimeTags, cfg: ConfigV3) -> tuple:
    def scen(name, df):
        return {
            "scenario": name,
            "round_trips": df.height,
            "net_pnl_rounded": round(float(df["net_pnl_rounded"].sum()), 2) if df.height else 0.0,
        }

    rows = [scen("all trades (primary)", t)]
    for mult in cfg.validation.dp_sensitivity:
        adj = (
            t.with_columns(net_pnl_rounded=pl.col("net_pnl_rounded") + pl.col("dp") * (1 - mult))
            if t.height
            else t
        )
        rows.append(scen(f"DP x {mult:g}", adj))
    rows.append(
        scen(
            f"entries from {cfg.validation.post_start}",
            t.filter(pl.col("entry_date") >= cfg.validation.post_start) if t.height else t,
        )
    )
    dd = drift.select("symbol", pl.col("date").alias("entry_date")).unique()
    rows.append(
        scen(
            "drift stock-day entries removed",
            t.join(dd, on=["symbol", "entry_date"], how="anti") if t.height else t,
        )
    )
    rd = tags.results.select("symbol", pl.col("date").alias("entry_date")).unique()
    on_res = t.join(rd, on=["symbol", "entry_date"]).height if t.height else 0
    rows.append(
        scen(
            "results-day entries removed",
            t.join(rd, on=["symbol", "entry_date"], how="anti") if t.height else t,
        )
    )
    share = (
        f"results-day share of entries: {on_res}/{t.height} ({on_res / t.height:.1%})"
        if t.height
        else ""
    )
    return pl.DataFrame(rows), share


def flag_counts(meta: dict, cfg: ConfigV3) -> pl.DataFrame:
    rows = []
    for name in cfg.execution.books:
        c = meta["books"].get(name, {}).get("counts", {})
        rows.append({"book": name, **{k.replace("exit:", ""): int(c.get(k, 0)) for k in FLAG_KEYS}})
    return pl.DataFrame(rows)


def build_report_v3(
    run_dir: str | Path, cfg: ConfigV3, tags: RegimeTags, drift_days: pl.DataFrame | None = None
) -> Path:
    run = Path(run_dir)
    meta = json.loads((run / "meta.json").read_text())
    trades = pl.read_parquet(run / "trades.parquet")
    daily = pl.read_parquet(run / "daily.parquet")
    labels = pl.read_parquet(run / "labels.parquet")
    finals = np.load(run / "null_final_equity.npy").tolist()
    null_ye = json.loads((run / "null_year_end.json").read_text())
    drift = (
        drift_days
        if drift_days is not None
        else pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date})
    )
    out = run / "report"
    out.mkdir(exist_ok=True)
    pb = cfg.execution.primary_book
    crit, verdict, ns, py, qt = criteria(
        meta, trades, finals, null_ye, labels, cfg, bool(meta.get("oos"))
    )
    (out / "criteria.json").write_text(
        json.dumps({"verdict": verdict, "criteria": [c.__dict__ for c in crit]}, indent=1)
    )
    csvio.write_csv(trades, out / "trade_log.csv")
    if (run / "signal_log.csv").exists():
        (out / "signal_log.csv").write_bytes((run / "signal_log.csv").read_bytes())
    bm = book_metrics(trades, daily, meta, cfg)
    csvio.write_csv(bm, out / "summary.csv")
    csvio.write_csv(py, out / "null_per_year.csv")
    csvio.write_csv(qt, out / "label_quintiles.csv")
    prim = trades.filter(pl.col("book") == pb) if trades.height else trades
    sd, share = secondary(prim, drift, tags, cfg)
    csvio.write_csv(sd, out / "secondary_diagnostics.csv")
    fc = flag_counts(meta, cfg)
    csvio.write_csv(fc, out / "flag_counts.csv")
    reg = regimes(prim, tags)
    for k, v in reg.items():
        csvio.write_csv(v, out / f"regime_{k}.csv")
    bl = meta.get("baselines", {})
    ibh = bl.get("index_buy_hold", {})
    ew = bl.get("equal_weight", {})
    base_rows = [
        {
            "baseline": "actual (primary book)",
            "final_equity": round(float(meta["books"][pb]["final_equity"]), 2),
        },
        {"baseline": "random-selection null (mean)", "final_equity": ns.get("null_mean")},
        {
            "baseline": "Nifty 200 buy-and-hold (price index, no costs)",
            "final_equity": round(float(ibh["final_equity"]), 2) if ibh else None,
        },
        {
            "baseline": "equal-weight weekly, % CNC costs (fractional shares)",
            "final_equity": round(float(ew["pct_costs"]["final_equity"]), 2) if ew else None,
        },
        {
            "baseline": "equal-weight weekly, % CNC costs + DP",
            "final_equity": round(float(ew["with_dp"]["final_equity"]), 2) if ew else None,
        },
    ]
    prov = meta.get("provenance") or {}
    if prov.get("pinned_by_this_run"):
        prov_line = (
            f"- code: commit `{prov['commit']}`; **this run pinned it** (first V3 in-sample run)"
        )
    elif prov.get("pinned_commit"):
        d = prov.get("diff_since_pin") or {}
        prov_line = (
            f"- code: pinned V3 commit `{prov['pinned_commit']}`; this run `{prov.get('commit')}`; "
            "result-relevant files changed since the pin: "
            f"{', '.join(d.get('relevant_files', [])) or 'none'}"
        )
    else:
        prov_line = "- code: no provenance recorded"
    ruin = {k: (v or {}).get("ruin_date") for k, v in meta["books"].items()}
    lines = [
        meta.get("survivorship_gap", "survivorship gap: n/a"),
        *[c.line() for c in crit],
        verdict,
        *(
            []
            if meta.get("prereg_match", True)
            else [
                "WARNING: config differs from docs/PREREGISTRATION_V3.md: not a pre-registered run"
            ]
        ),
        f"capital model: cash ledger per book from Rs {cfg.sizing.starting_capital:,.0f}; "
        f"Slot_w = {cfg.sizing.slot_fraction:.0%} x equity_w / {cfg.sizing.slots}; "
        f"no new buys once equity_w < Rs {cfg.sizing.ruin_equity:,.0f}",
        "ruin dates: " + ", ".join(f"{k} {ruin.get(k) or 'never'}" for k in cfg.execution.books),
        "results are PRE-TAX (taxes ignored); long-only in a rising market: b can pass on "
        "beta alone, c and d are the real test",
        "",
        f"# V3 backtest report: run {meta.get('run_id')}",
        "",
        f"- period: {meta.get('start')} .. {meta.get('end')} "
        f"({'OUT-OF-SAMPLE' if meta.get('oos') else 'in-sample'})",
        f"- config hash: `{meta.get('config_hash')}`",
        f"- data version: `{meta.get('data_version')}` "
        f"(vendor snapshot {meta.get('vendor_snapshot_id')})",
        f"- primary book: `{pb}`, rupee-rounded costs; stress: `{cfg.execution.stress_book}`; "
        "CNC costs unverified",
        prov_line,
        "",
        "## Metrics per book",
        _md(bm),
        "## Nulls",
        "### Primary: random selection (criterion c)",
        "```",
        json.dumps(ns, indent=1),
        "```",
        "### Annual net vs the null's mean (criterion d)",
        _md(py),
        "### Secondary baselines (final equity, Rs)",
        _md(pl.DataFrame(base_rows)),
        "## Secondary diagnostics (excluded from pass/fail)",
        "_Trades removed from the existing primary book; the ledger is not re-run._",
        _md(sd),
        share,
        "",
    ]
    for k, v in reg.items():
        lines += [f"### {k}", _md(v)]
    lines += [
        "## Flag counts",
        _md(fc),
        "## Criterion e: next-week label return by r_week quintile (top first, informational)",
        _md(qt),
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    return out
