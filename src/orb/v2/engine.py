"""V2 backtest engine.

For each trading day:
  1. ``scan_day_v2`` at 09:45: z, qualification, ranks, top-3 selection; the
     index signal and the index-variant selection.
  2. For every book (each slippage model, plus the index variant on primary
     slippage), from that book's own equity: Slot_d, the ruin stop, then
     ``simulate_v2`` for each selected stock (circuit guard for non-F&O stocks).
  3. Contract-note rounding of STT / stamp over the book's taken orders that day;
     equity_{d+1} = equity_d + sum of rounded net.
  4. Invariants per (day, book); labels: every qualifying stock simulated as if
     taken at the primary book's Slot_d under primary slippage.

The OOS period is locked as in V1 (``--oos`` + a one-time ledger, per strategy).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import polars as pl

from orb import csvio
from orb.context import DayInputs
from orb.engine import RunResult, _check_oos, _record_oos
from orb.refdata.fno import FnoCalendar
from orb.sim.costs import Order, charges_frame
from orb.v2 import invariants
from orb.v2.config import ConfigV2
from orb.v2.context import V2ContextBuilder
from orb.v2.signal import SIGNAL_SCHEMA, index_selection, index_signal, scan_day_v2
from orb.v2.sizing import DayLimits, EquityBook
from orb.v2.trade import OK, TradeV2, simulate_v2

log = logging.getLogger(__name__)

TAKEN, SKIPPED = "TAKEN", "SKIPPED"
RUIN = "RUIN"
INDEX_BOOK = "index_variant"


@dataclass
class RunResultV2:
    signals: pl.DataFrame  # every member stock-day at 09:45
    book: pl.DataFrame  # every selected stock x book (taken or skipped)
    equity: pl.DataFrame  # date x book
    labels: pl.DataFrame  # every qualifying stock, as if taken (primary slippage)
    index: pl.DataFrame  # index signal per day
    coverage: pl.DataFrame
    ruin: dict = field(default_factory=dict)  # book -> first ruin date
    meta: dict = field(default_factory=dict)

    def _v1(self) -> RunResult:
        return RunResult(pl.DataFrame(), pl.DataFrame(), pl.DataFrame(), self.coverage)

    def survivorship_gap(self) -> pl.DataFrame:
        return self._v1().survivorship_gap()

    def header(self) -> str:
        return self._v1().header()


class EngineV2:
    def __init__(
        self,
        cfg: ConfigV2,
        builder: V2ContextBuilder,
        ticks: dict,
        fno: FnoCalendar | None,
        index_ctx: dict,
    ):
        """``ticks``: {(symbol, date): tick}; ``fno``: F&O membership (None = no stock
        is in F&O, so the guard applies everywhere); ``index_ctx``: {(index, date):
        daily-context row}."""
        self.cfg = cfg
        self.builder = builder
        self.ticks = ticks
        self.fno = fno
        self.index_ctx = index_ctx
        self.models = dict(cfg.execution.books)
        self.books = {
            **{name: EquityBook(name, cfg) for name in self.models},
            INDEX_BOOK: EquityBook(INDEX_BOOK, cfg),
        }

    def _guard(self, symbol: str, day: date) -> bool:
        if not self.cfg.circuit_guard.enabled:
            return False
        return not (self.fno is not None and self.fno.is_fno(symbol, day))

    def _trade(self, inputs: DayInputs, bars, row: dict, lim: DayLimits, model) -> TradeV2:
        return simulate_v2(
            bars[row["symbol"]],
            inputs.day,
            row["side"],
            lim.slot_value,
            self.ticks.get((row["symbol"], inputs.day)),
            model,
            self._guard(row["symbol"], inputs.day),
            self.cfg,
        )

    def _round(self, day: date, taken: list[dict]) -> None:
        """Contract-note rounding of the day's taken orders in one book (in place)."""
        if not taken:
            return
        orders = []
        for t in taken:
            buy, sell = ("buy", "sell") if t["side"] == "long" else ("sell", "buy")
            orders += [
                Order(day, buy, t["qty"], t["entry_price"], t["symbol"] + ":in"),
                Order(day, sell, t["qty"], t["exit_price"], t["symbol"] + ":out"),
            ]
        round_ = self.cfg.execution.round_stt_stamp_to_rupee
        cf = charges_frame(orders, self.cfg.costs, round_stt_stamp=round_)
        per = (
            cf.with_columns(symbol=pl.col("order_id").str.split(":").list.first())
            .group_by("symbol")
            .agg(pl.col("total").sum())
        )
        tot = dict(per.iter_rows())
        for t in taken:
            t["costs_rounded"] = tot[t["symbol"]]
            t["net_pnl_rounded"] = t["gross_pnl"] - t["costs_rounded"]

    def _book_day(self, inputs: DayInputs, bars, name: str, cands: list[dict]) -> tuple:
        eb = self.books[name]
        lim = eb.limits(inputs.day)
        model = self.models.get(name, self.models[self.cfg.execution.primary_book])
        rows, taken = [], []
        for c in cands:
            base = {
                "date": inputs.day,
                "symbol": c["symbol"],
                "book": name,
                "z": c["z"],
                "rank": c.get("rank"),
                "alignment": c.get("alignment"),
                "equity_d": lim.equity,
                "slot_value": lim.slot_value,
                "guard": self._guard(c["symbol"], inputs.day),
            }
            if lim.ruined:
                rows.append(
                    {**base, "side": c["side"], "book_decision": SKIPPED, "book_reason": RUIN}
                )
                continue
            t = self._trade(inputs, bars, c, lim, model)
            r = {**base, **t.as_dict()}
            r["book_decision"], r["book_reason"] = (
                (TAKEN, None) if t.status == OK else (SKIPPED, t.status)
            )
            rows.append(r)
            if t.status == OK:
                taken.append(r)
        self._round(inputs.day, taken)
        invariants.check(taken, lim, name, self.cfg)
        day_net = sum(t["net_pnl_rounded"] for t in taken)
        eq_row = {
            "date": inputs.day,
            "book": name,
            "equity_start": lim.equity,
            "deployable": lim.deployable,
            "slot_value": lim.slot_value,
            "ruined": lim.ruined,
            "taken": len(taken),
            "day_net": sum(t["net_pnl"] for t in taken),
            "day_net_rounded": day_net,
            "equity_end": eb.close_day(inputs.day, day_net),
        }
        return rows, eq_row, lim

    def _labels(self, inputs: DayInputs, bars, sig: pl.DataFrame, lim: DayLimits) -> list[dict]:
        model = self.models[self.cfg.execution.primary_book]
        out = []
        for c in sig.filter(pl.col("qualified")).to_dicts():
            t = self._trade(inputs, bars, c, lim, model)
            out.append(
                {
                    "date": inputs.day,
                    "symbol": c["symbol"],
                    "z": c["z"],
                    "abs_z": abs(c["z"]),
                    "rank": c["rank"],
                    "selected": c["selected"],
                    "slot_value": lim.slot_value,
                    **{k: v for k, v in t.as_dict().items()},
                    "ret_net": (t.net_pnl / t.notional) if t.status == OK else None,
                    "ret_gross": (t.gross_pnl / t.notional) if t.status == OK else None,
                }
            )
        return out

    def run(
        self,
        start: date,
        end: date,
        oos: bool = False,
        force_reason: str | None = None,
        ledger: Path | None = None,
    ) -> RunResultV2:
        ledger = ledger or Path(self.cfg.data.root) / "_runs" / "v2" / "oos_ledger.jsonl"
        start, end = _check_oos(self.cfg, start, end, oos, ledger, force_reason)
        days = [d for d in self.builder.calendar if start <= d <= end]
        sig_parts, book_rows, eq_rows, label_rows, idx_rows, cov = [], [], [], [], [], []
        top = self.cfg.selection.max_positions
        primary = self.cfg.execution.primary_book
        for d in days:
            inputs = self.builder.day(d)
            if d not in self.builder.excluded_all:
                cov.append(
                    {
                        "date": d,
                        "members": len(inputs.stocks) + len(inputs.no_data),
                        "no_data": len(inputs.no_data),
                    }
                )
            sig = scan_day_v2(inputs, self.cfg)
            sig_parts.append(sig)
            idx = index_signal(inputs, self.index_ctx, self.cfg)
            idx_rows.append(idx)
            bars = {sd.symbol: sd.bars for sd in inputs.stocks}
            selected = sig.filter(pl.col("selected")).sort("rank").to_dicts()
            for name in self.models:
                rows, eq, lim = self._book_day(inputs, bars, name, selected)
                book_rows += rows
                eq_rows.append(eq)
                if name == primary:
                    label_rows += self._labels(inputs, bars, sig, lim)
            isel = index_selection(sig, idx, top).to_dicts()
            rows, eq, _ = self._book_day(inputs, bars, INDEX_BOOK, isel)
            book_rows += rows
            eq_rows.append(eq)
        equity = pl.DataFrame(eq_rows, infer_schema_length=None) if eq_rows else pl.DataFrame()
        for name in self.books:
            if equity.height:
                invariants.check_equity_path(
                    equity.filter(pl.col("book") == name).sort("date").to_dicts(), self.cfg
                )
        res = RunResultV2(
            pl.concat(sig_parts) if sig_parts else pl.DataFrame(schema=SIGNAL_SCHEMA),
            pl.DataFrame(book_rows, infer_schema_length=None) if book_rows else pl.DataFrame(),
            equity,
            pl.DataFrame(label_rows, infer_schema_length=None) if label_rows else pl.DataFrame(),
            pl.DataFrame(idx_rows, infer_schema_length=None) if idx_rows else pl.DataFrame(),
            pl.DataFrame(cov, schema={"date": pl.Date, "members": pl.Int64, "no_data": pl.Int64}),
            ruin={n: b.ruin_date for n, b in self.books.items()},
        )
        res.meta = {
            "strategy": "v2",
            "start": start,
            "end": end,
            "oos": end >= self.cfg.run.oos_start,
        }
        if end >= self.cfg.run.oos_start:
            _record_oos(
                ledger,
                {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "strategy": "v2",
                    "start": start,
                    "end": end,
                    "config_hash": self.cfg.hash(),
                    "forced_reason": force_reason,
                },
            )
        return res


def signal_log(res: RunResultV2, book: str) -> pl.DataFrame:
    """Every stock-day at 09:45 plus the trade fields of ``book``'s taken trades."""
    keep = [
        "date",
        "symbol",
        "side",
        "book_decision",
        "book_reason",
        "qty",
        "entry_price",
        "exit_price",
        "exit_reason",
        "gross_pnl",
        "costs",
        "costs_rounded",
        "slippage_paid",
        "net_pnl",
        "net_pnl_rounded",
        "flags",
    ]
    sig = res.signals
    if res.book.height == 0:
        return sig
    b = res.book.filter(pl.col("book") == book)
    b = b.select([c for c in keep if c in b.columns]).rename({"side": "trade_side"})
    out = sig.join(b, on=["date", "symbol"], how="left")
    return out.rename({"entry_price": "entry", "exit_price": "exit", "slippage_paid": "slippage"})


def write_run_v2(res: RunResultV2, cfg: ConfigV2, out_root: str | Path, meta: dict) -> Path:
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "_" + cfg.hash()[:8]
    d = Path(out_root) / run_id
    d.mkdir(parents=True, exist_ok=True)
    res.signals.write_parquet(d / "signals.parquet")
    res.book.write_parquet(d / "book.parquet")
    res.equity.write_parquet(d / "equity.parquet")
    res.labels.write_parquet(d / "labels.parquet")
    res.index.write_parquet(d / "index.parquet")
    res.coverage.write_parquet(d / "coverage.parquet")
    csvio.write_csv(res.survivorship_gap(), d / "survivorship_gap.csv")
    csvio.write_csv(signal_log(res, cfg.execution.primary_book), d / "signal_log.csv")
    meta = {
        **res.meta,
        **meta,
        "run_id": run_id,
        "survivorship_gap": res.header(),
        "ruin": {k: (v.isoformat() if v else None) for k, v in res.ruin.items()},
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True, default=str))
    (d / "config.json").write_text(json.dumps(cfg.model_dump(mode="json"), indent=1))
    (d / "summary.txt").write_text(f"{res.header()}\nrun {run_id}\n\n{summarize_v2(res.book)}\n")
    return d


def summarize_v2(book: pl.DataFrame) -> pl.DataFrame:
    if book.height == 0 or "net_pnl" not in book.columns:
        return pl.DataFrame()
    t = book.filter(pl.col("book_decision") == TAKEN)
    return (
        t.group_by("book")
        .agg(
            trades=pl.len(),
            gross_pnl=pl.col("gross_pnl").sum().round(2),
            costs=pl.col("costs").sum().round(2),
            net_pnl=pl.col("net_pnl").sum().round(2),
            net_pnl_rounded=pl.col("net_pnl_rounded").sum().round(2),
            hit_rate=(pl.col("net_pnl") > 0).mean().round(4),
        )
        .sort("book")
    )
