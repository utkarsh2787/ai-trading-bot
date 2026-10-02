"""Backtest engine: scan -> simulate every signal -> allocate -> book.

For each trading day:
  1. ``ContextBuilder.day`` + ``scan_day``: one row per first breakout (signals).
  2. ``simulate`` every signal for every (variant, slippage multiplier), sized as
     if taken. These are also the labels used by deliverable 5.
  3. ``allocate`` per (variant, multiplier): slots, ties, deployment.
  4. Costs for taken trades with and without contract-note rounding of
     STT/stamp (per day, per run).

The engine depends on the scorer only through the ``Scorer`` protocol, and the
out-of-sample period is locked behind ``oos=True`` plus a one-time ledger.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import polars as pl

from orb.config import Config
from orb.context import ContextBuilder, DayInputs
from orb.portfolio import TAKEN, allocate
from orb.scoring.base import Scorer
from orb.signals import CANDIDATE_SCHEMA, scan_day
from orb.sim.costs import Order, charges_frame
from orb.sim.trade import TradeResult, simulate

log = logging.getLogger(__name__)

VARIANTS = {False: "default", True: "conservative"}


class OOSLockError(RuntimeError):
    pass


@dataclass
class RunResult:
    signals: pl.DataFrame
    sims: pl.DataFrame  # signal key x variant x slippage_mult
    book: pl.DataFrame  # sims + book_decision / book_reason + rounded costs
    coverage: pl.DataFrame  # date, members, no_data
    meta: dict = field(default_factory=dict)

    def survivorship_gap(self) -> pl.DataFrame:
        c = self.coverage.with_columns(year=pl.col("date").dt.year().cast(pl.String))
        by = (
            c.group_by("year")
            .agg(eligible_days=pl.col("members").sum(), missing_days=pl.col("no_data").sum())
            .sort("year")
        )
        tot = pl.DataFrame(
            {
                "year": ["ALL"],
                "eligible_days": [c["members"].sum()],
                "missing_days": [c["no_data"].sum()],
            }
        )
        out = pl.concat([by, tot.cast(by.schema)])
        return out.with_columns(
            gap_pct=(100 * pl.col("missing_days") / pl.col("eligible_days")).round(3)
        )

    def header(self) -> str:
        g = self.survivorship_gap().filter(pl.col("year") == "ALL").row(0, named=True)
        pct = g["gap_pct"] if g["eligible_days"] else float("nan")
        return (
            f"survivorship gap: {pct:.2f}% of eligible stock-days have no 1-min data "
            f"({g['missing_days']}/{g['eligible_days']})"
        )


def _check_oos(
    cfg: Config, start: date, end: date, oos: bool, ledger: Path, force_reason: str | None
) -> tuple[date, date]:
    if end < cfg.run.oos_start:
        return start, end
    if not oos:
        raise OOSLockError(
            f"{start}..{end} reaches the locked OOS period (from {cfg.run.oos_start}); "
            "pass oos=True (--oos) to run it once"
        )
    if ledger.exists() and ledger.read_text().strip() and not force_reason:
        raise OOSLockError(f"OOS already run (see {ledger}); a rerun needs a logged reason")
    return start, end


def _record_oos(ledger: Path, entry: dict) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")


class Engine:
    def __init__(self, cfg: Config, builder: ContextBuilder, ticks: dict, scorer: Scorer):
        """``ticks``: {(symbol, date): tick} from ``sim.ticks.daily_ticks``."""
        self.cfg = cfg
        self.builder = builder
        self.ticks = ticks
        self.scorer = scorer

    # ---------------------------------------------------------------- day
    def _simulate_day(self, inputs: DayInputs, signals: pl.DataFrame) -> list[dict]:
        bars = {sd.symbol: sd.bars for sd in inputs.stocks}
        rows = []
        for sig in signals.to_dicts():
            for conservative in self.cfg.execution.entry_candle_stop_check_variants:
                for mult in self.cfg.execution.stress_multipliers:
                    tr: TradeResult = simulate(
                        bars[sig["symbol"]],
                        inputs.day,
                        sig["signal_slot"],
                        sig["side"],
                        sig["or_h"],
                        sig["or_l"],
                        sig["score"],
                        self.ticks.get((sig["symbol"], inputs.day)),
                        self.cfg,
                        VARIANTS[conservative],
                        float(mult),
                    )
                    rows.append({"date": inputs.day, "symbol": sig["symbol"], **tr.as_dict()})
        return rows

    def _book_day(self, signals: pl.DataFrame, sims: pl.DataFrame) -> pl.DataFrame:
        parts = []
        keep = ["date", "symbol", "side", "score", "decision", "reason"]
        for _, g in sims.group_by("variant", "slippage_mult", maintain_order=True):
            day = g.join(signals.select(keep), on=["date", "symbol"])
            booked = allocate(day, self.cfg.portfolio)
            parts.append(self._round_costs(booked))
        return pl.concat(parts, how="diagonal") if parts else pl.DataFrame()

    def _round_costs(self, booked: pl.DataFrame) -> pl.DataFrame:
        """Contract-note rounding over the day's taken orders (this run only)."""
        taken = booked.filter(pl.col("book_decision") == TAKEN).to_dicts()
        orders = []
        for t in taken:
            buy, sell = ("buy", "sell") if t["side"] == "long" else ("sell", "buy")
            orders += [
                Order(t["date"], buy, t["qty"], t["entry_price"], t["symbol"] + ":in"),
                Order(t["date"], sell, t["qty"], t["exit_price"], t["symbol"] + ":out"),
            ]
        if not orders:
            return booked.with_columns(
                costs_rounded=pl.lit(None, pl.Float64), net_pnl_rounded=pl.lit(None, pl.Float64)
            )
        cf = (
            charges_frame(orders, self.cfg.costs, round_stt_stamp=True)
            .with_columns(symbol=pl.col("order_id").str.split(":").list.first())
            .group_by("symbol")
            .agg(costs_rounded=pl.col("total").sum())
        )
        return (
            booked.join(cf, on="symbol", how="left")
            .with_columns(
                costs_rounded=pl.when(pl.col("book_decision") == TAKEN).then("costs_rounded"),
            )
            .with_columns(net_pnl_rounded=pl.col("gross_pnl") - pl.col("costs_rounded"))
        )

    # ---------------------------------------------------------------- run
    def run(
        self,
        start: date,
        end: date,
        oos: bool = False,
        force_reason: str | None = None,
        ledger: Path | None = None,
    ) -> RunResult:
        ledger = ledger or Path(self.cfg.data.root) / "_runs" / "oos_ledger.jsonl"
        start, end = _check_oos(self.cfg, start, end, oos, ledger, force_reason)
        days = [d for d in self.builder.calendar if start <= d <= end]
        sig_parts, sim_rows, book_parts, cov = [], [], [], []
        for d in days:
            inputs = self.builder.day(d)
            cov.append(
                {
                    "date": d,
                    "members": len(inputs.stocks) + len(inputs.no_data),
                    "no_data": len(inputs.no_data),
                }
            )
            signals = scan_day(inputs, self.cfg, self.scorer)
            if signals.height == 0:
                continue
            rows = self._simulate_day(inputs, signals)
            sims = pl.DataFrame(rows, infer_schema_length=None)
            sig_parts.append(signals)
            sim_rows += rows
            book_parts.append(self._book_day(signals, sims))
        signals = pl.concat(sig_parts) if sig_parts else pl.DataFrame(schema=CANDIDATE_SCHEMA)
        sims = pl.DataFrame(sim_rows, infer_schema_length=None) if sim_rows else pl.DataFrame()
        book = pl.concat(book_parts, how="diagonal") if book_parts else pl.DataFrame()
        res = RunResult(
            signals,
            sims,
            book,
            pl.DataFrame(cov, schema={"date": pl.Date, "members": pl.Int64, "no_data": pl.Int64}),
        )
        if end >= self.cfg.run.oos_start:
            _record_oos(
                ledger,
                {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "start": start,
                    "end": end,
                    "config_hash": self.cfg.hash(),
                    "forced_reason": force_reason,
                },
            )
        return res


def summarize(book: pl.DataFrame) -> pl.DataFrame:
    """Per (variant, slippage) headline numbers for taken trades."""
    if book.height == 0:
        return pl.DataFrame()
    t = book.filter(pl.col("book_decision") == TAKEN)
    return (
        t.group_by("variant", "slippage_mult")
        .agg(
            trades=pl.len(),
            gross_pnl=pl.col("gross_pnl").cast(pl.Float64).sum().round(2),
            costs=pl.col("costs").cast(pl.Float64).sum().round(2),
            net_pnl=pl.col("net_pnl").cast(pl.Float64).sum().round(2),
            net_pnl_rounded=pl.col("net_pnl_rounded").cast(pl.Float64).sum().round(2),
            slippage_paid=pl.col("slippage_paid").cast(pl.Float64).sum().round(2),
            hit_rate=(pl.col("net_pnl").cast(pl.Float64) > 0).mean().round(4),
            avg_r_net=pl.col("r_net").cast(pl.Float64).mean().round(4),
        )
        .sort("variant", "slippage_mult")
    )


def write_run(res: RunResult, cfg: Config, out_root: str | Path, meta: dict) -> Path:
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "_" + cfg.hash()[:8]
    d = Path(out_root) / run_id
    d.mkdir(parents=True, exist_ok=True)
    res.signals.write_parquet(d / "signals.parquet")
    res.sims.write_parquet(d / "sims.parquet")
    res.book.write_parquet(d / "book.parquet")
    res.survivorship_gap().write_csv(d / "survivorship_gap.csv")
    summary = summarize(res.book)
    meta = {**meta, "run_id": run_id, "survivorship_gap": res.header()}
    (d / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True, default=str))
    (d / "config.json").write_text(json.dumps(cfg.model_dump(mode="json"), indent=1))
    (d / "summary.txt").write_text(f"{res.header()}\nrun {run_id}\n\n{summary}\n")
    return d
