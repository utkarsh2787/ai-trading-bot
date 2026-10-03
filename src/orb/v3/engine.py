"""V3 backtest engine.

  1. precompute: scan every rebalance day (signals), memoise fills / valuations
  2. books: one ledger per slippage model (primary, primary_2x, v1_model)
  3. labels: next-week returns of every eligible stock (gross; net at the primary
     book's Slot_w)
  4. primary null: 2,000 random-selection ledgers (same code, same rules)
  5. secondary baselines: Nifty 200 buy-and-hold; equal-weight universe weekly
The OOS period is locked as in V1 / V2 (``--oos`` + a one-time ledger, per strategy).
"""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from orb import csvio
from orb.engine import RunResult, _check_oos, _record_oos
from orb.v2.trade import fill
from orb.v3.config import ConfigV3
from orb.v3.costs import CnCOrder, dp_charge, order_charges, schedule_on
from orb.v3.data import V3Data
from orb.v3.ledger import Ledger, LedgerResult
from orb.v3.market import Market
from orb.v3.signal import NO_MINUTE_DATA, SIGNAL_SCHEMA

log = logging.getLogger(__name__)


@dataclass
class RunResultV3:
    signals: pl.DataFrame
    trades: pl.DataFrame
    orders: pl.DataFrame
    daily: pl.DataFrame
    rebalances: pl.DataFrame
    labels: pl.DataFrame
    coverage: pl.DataFrame
    books: dict = field(default_factory=dict)  # book -> {ruin_date, final_equity, year_end, counts}
    null: dict = field(default_factory=dict)
    baselines: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def _v1(self) -> RunResult:
        return RunResult(pl.DataFrame(), pl.DataFrame(), pl.DataFrame(), self.coverage)

    def survivorship_gap(self) -> pl.DataFrame:
        return self._v1().survivorship_gap()

    def header(self) -> str:
        return self._v1().header() + " [rebalance days]"


def _frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


class EngineV3:
    def __init__(self, cfg: ConfigV3, data: V3Data, index_daily: pl.DataFrame | None = None):
        self.cfg = cfg
        self.data = data
        self.market = Market(data)
        self.index_daily = index_daily

    # ------------------------------------------------------------ labels
    def labels(self, rebs: list[date], slot_w: dict[date, float]) -> pl.DataFrame:
        cfg, data, m = self.cfg, self.data, self.market
        model = cfg.execution.books[cfg.execution.primary_book]
        rows = []
        for d, n in zip(rebs, rebs[1:], strict=False):
            sig = m.scan(d).filter(pl.col("eligible"))
            for s, rw in sig.select("symbol", "r_week").iter_rows():
                r = {
                    "date": d,
                    "next": n,
                    "symbol": s,
                    "r_week": rw,
                    "ret_gross": None,
                    "ret_net": None,
                }
                e_raw, x_raw = m.open_raw(s, d), m.open_raw(s, n)
                if e_raw and x_raw and (data.delisted_on(s) is None or data.delisted_on(s) >= n):
                    f = data.factor_between(s, d, n)
                    div = 0.0
                    for cd, amt in data.div_by_symbol.get(s, []):
                        if d < cd <= n:
                            div += amt / data.factor_between(s, d, cd - timedelta(days=1))
                    r["ret_gross"] = (x_raw / f + div) / e_raw - 1
                    te, tx = (
                        data.ticks.get((s, d)),
                        data.ticks.get((s, n)) or data.ticks.get((s, d)),
                    )
                    if te and tx and slot_w.get(d):
                        ef = fill(e_raw, "buy", te, model)
                        q = math.floor(slot_w[d] / ef + 1e-9)
                        if q >= 1:
                            xq = math.floor(q / f + 1e-9)
                            xf = fill(x_raw, "sell", tx, model)
                            cost = sum(order_charges(CnCOrder(d, "buy", q, ef), cfg.costs).values())
                            cost += sum(
                                order_charges(CnCOrder(n, "sell", xq, xf), cfg.costs).values()
                            )
                            cost += dp_charge(cfg.costs, n)
                            r["ret_net"] = (xq * xf + q * div - q * ef - cost) / (q * ef)
                rows.append(r)
        return _frame(rows)

    # -------------------------------------------------------------- null
    def null(self, start: date, end: date, draws: int) -> dict:
        cfg, m = self.cfg, self.market
        model = cfg.execution.books[cfg.execution.primary_book]
        k = cfg.sizing.slots
        finals, years, ruins, trips = [], [], 0, []
        for i in range(draws):
            rng = random.Random(cfg.run.seed * 10_000 + i)

            def pick(d, rng=rng):
                el = m.eligible(d)
                return rng.sample(el, min(k, len(el)))

            res = Ledger(m, model, f"null{i}", pick, record=False).run(start, end)
            finals.append(res.final_equity)
            years.append(res.year_end)
            ruins += res.ruin_date is not None
            trips.append(res.counts["ROUND_TRIPS"])
            if (i + 1) % 200 == 0:
                log.info("null: %d / %d draws", i + 1, draws)
        return {
            "final_equity": finals,
            "year_end": years,
            "ruined_draws": ruins,
            "round_trips": trips,
        }

    # --------------------------------------------------------- baselines
    def index_buy_hold(self, start: date, end: date) -> dict:
        if self.index_daily is None or self.index_daily.height == 0:
            return {}
        ix = self.index_daily.filter(pl.col("date").is_between(start, end)).sort("date")
        c0, c1 = ix["close"][0], ix["close"][-1]
        cap = self.cfg.sizing.starting_capital
        ye = ix.group_by(pl.col("date").dt.year().alias("y")).agg(pl.col("close").last()).sort("y")
        return {
            "start": ix["date"][0],
            "end": ix["date"][-1],
            "final_equity": cap * c1 / c0,
            "year_end": {y: cap * c / c0 for y, c in ye.iter_rows()},
        }

    def equal_weight(self, labels: pl.DataFrame) -> dict:
        """Fractional shares, all eligible stocks at equal weight, rebalanced weekly
        at the 15:00 open on gross label returns; percentage CNC costs on turnover,
        plus (second version) the DP charge per stock with a net sale."""
        cfg = self.cfg
        if labels.height == 0:
            return {}
        out = {}
        for with_dp in (False, True):
            eq = cfg.sizing.starting_capital
            held: dict[str, float] = {}  # symbol -> rupee value
            ye: dict[int, float] = {}
            for (d,), g in labels.filter(pl.col("ret_gross").is_not_null()).group_by(
                "date", maintain_order=True
            ):
                sc = schedule_on(cfg.costs, d)
                buy_rate = (
                    sc.stt_buy_pct
                    + sc.stamp_buy_pct
                    + (sc.exchange_txn_pct + sc.sebi_per_crore / 1e7) * (1 + sc.gst_pct)
                )
                sell_rate = sc.stt_sell_pct + (sc.exchange_txn_pct + sc.sebi_per_crore / 1e7) * (
                    1 + sc.gst_pct
                )
                syms = g["symbol"].to_list()
                eq = sum(held.values()) if held else eq
                target = {s: eq / len(syms) for s in syms}
                buys = sum(
                    max(0.0, target.get(s, 0) - held.get(s, 0)) for s in set(target) | set(held)
                )
                sells = sum(
                    max(0.0, held.get(s, 0) - target.get(s, 0)) for s in set(target) | set(held)
                )
                n_sold = sum(1 for s in held if held[s] - target.get(s, 0) > 1e-9)
                cost = (
                    buys * buy_rate
                    + sells * sell_rate
                    + (n_sold * sc.dp_per_scrip_sell_day if with_dp else 0)
                )
                scale = (eq - cost) / eq if eq > 0 else 0
                held = {
                    s: v * scale * (1 + r)
                    for s, v, r in zip(
                        syms, [target[s] for s in syms], g["ret_gross"].to_list(), strict=True
                    )
                }
                ye[d.year] = sum(held.values())
            eq = sum(held.values())
            if held:
                sc = schedule_on(cfg.costs, d)
                sell_rate = sc.stt_sell_pct + (sc.exchange_txn_pct + sc.sebi_per_crore / 1e7) * (
                    1 + sc.gst_pct
                )
                eq -= eq * sell_rate + (len(held) * sc.dp_per_scrip_sell_day if with_dp else 0)
                ye[d.year] = eq
            out["with_dp" if with_dp else "pct_costs"] = {"final_equity": eq, "year_end": ye}
        return out

    # --------------------------------------------------------------- run
    def run(
        self,
        start: date,
        end: date,
        oos: bool = False,
        force_reason: str | None = None,
        ledger: Path | None = None,
        null_draws: int | None = None,
    ) -> RunResultV3:
        cfg, m = self.cfg, self.market
        ledger = ledger or Path(cfg.data.root) / "_runs" / "v3" / "oos_ledger.jsonl"
        start, end = _check_oos(cfg, start, end, oos, ledger, force_reason)
        rebs = m.rebalances(start, end)
        log.info("V3: %d rebalance days %s .. %s; precomputing", len(rebs), rebs[0], rebs[-1])
        m.precompute(rebs, forced_day=rebs[-1])
        signals = (
            pl.concat([m.scan(d) for d in rebs]) if rebs else pl.DataFrame(schema=SIGNAL_SCHEMA)
        )
        cov = (
            signals.group_by("date")
            .agg(members=pl.len(), no_data=(pl.col("decision") == NO_MINUTE_DATA).sum())
            .with_columns(pl.col("members", "no_data").cast(pl.Int64))
            .sort("date")
        )
        books, results = {}, {}
        for name, model in cfg.execution.books.items():
            log.info("V3: book %s", name)
            r: LedgerResult = Ledger(m, model, name, m.targets).run(start, end)
            results[name] = r
            books[name] = {
                "ruin_date": r.ruin_date,
                "final_equity": r.final_equity,
                "year_end": r.year_end,
                "counts": dict(r.counts),
            }
        prim = results[cfg.execution.primary_book]
        slot_w = {x["date"]: x["slot_w"] for x in prim.rebalances}
        labels = self.labels(rebs, slot_w)
        draws = cfg.validation.null_draws if null_draws is None else null_draws
        log.info("V3: random-selection null, %d draws", draws)
        null = self.null(start, end, draws)
        baselines = {
            "index_buy_hold": self.index_buy_hold(start, end),
            "equal_weight": self.equal_weight(labels),
        }
        res = RunResultV3(
            signals,
            _frame([t for r in results.values() for t in r.trades]),
            _frame([o for r in results.values() for o in r.orders]),
            _frame([x for r in results.values() for x in r.daily]),
            _frame([x for r in results.values() for x in r.rebalances]),
            labels,
            cov,
            books,
            null,
            baselines,
        )
        res.meta = {
            "strategy": "v3",
            "start": start,
            "end": end,
            "oos": end >= cfg.run.oos_start,
            "null_draws": draws,
        }
        if end >= cfg.run.oos_start:
            _record_oos(
                ledger,
                {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "strategy": "v3",
                    "start": start,
                    "end": end,
                    "config_hash": cfg.hash(),
                    "forced_reason": force_reason,
                },
            )
        return res


def write_run_v3(res: RunResultV3, cfg: ConfigV3, out_root: str | Path, meta: dict) -> Path:
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "_" + cfg.hash()[:8]
    d = Path(out_root) / run_id
    d.mkdir(parents=True, exist_ok=True)
    for name in ("signals", "trades", "daily", "rebalances", "labels", "coverage"):
        getattr(res, name).write_parquet(d / f"{name}.parquet")
    orders = res.orders
    if orders.height:
        orders = orders.with_columns(
            pl.col("flags").cast(pl.List(pl.String)).list.join(";"),
            *[
                pl.col("charges").struct.field(k).alias(f"charge_{k}")
                for k in ("brokerage", "stt", "exchange_txn", "stamp", "sebi_fee", "gst")
            ],
        ).drop("charges")
    orders.write_parquet(d / "orders.parquet")
    csvio.write_csv(res.survivorship_gap(), d / "survivorship_gap.csv")
    csvio.write_csv(res.signals, d / "signal_log.csv")
    nl = res.null
    np.save(d / "null_final_equity.npy", np.asarray(nl.get("final_equity", []), dtype=float))
    (d / "null_year_end.json").write_text(json.dumps(nl.get("year_end", []), default=str))
    meta = {
        **res.meta,
        **meta,
        "run_id": run_id,
        "survivorship_gap": res.header(),
        "books": res.books,
        "null_summary": {
            "ruined_draws": nl.get("ruined_draws"),
            "mean_round_trips": float(np.mean(nl["round_trips"]))
            if nl.get("round_trips")
            else None,
        },
        "baselines": res.baselines,
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True, default=str))
    (d / "config.json").write_text(json.dumps(cfg.model_dump(mode="json"), indent=1))
    t = res.trades
    summ = (
        t.group_by("book")
        .agg(trips=pl.len(), net_rounded=pl.col("net_pnl_rounded").sum().round(2))
        .sort("book")
        if t.height
        else t
    )
    (d / "summary.txt").write_text(f"{res.header()}\nrun {run_id}\n\n{summ}\n")
    return d
