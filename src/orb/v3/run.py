"""``orb backtest --strategy v3`` / ``orb report --strategy v3``.

Inputs: the raw store (official daily bars across renames, 1-min bars), the
reference files, the DQ exclusions in ``data/_dq`` (FNO_BAN dropped inside V3),
``corporate_actions.csv`` with its subject text (dividends), the tick table and
``fno_stocks.csv``. The gate: review rows, clean tree, logged changes since the
V3 pin, config hash vs docs/PREREGISTRATION_V3.md; the first in-sample run pins
the V3 commit.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from orb import csvio
from orb.v3.config import ConfigV3

log = logging.getLogger("orb")

STRATEGY = "v3"
PREREG = "docs/PREREGISTRATION_V3.md"


def load_inputs(cfg: ConfigV3):
    from orb.context import ContextBuilder
    from orb.data.pipeline import Stores, bhav_for
    from orb.data.reference import load_reference
    from orb.refdata.fno import FnoCalendar
    from orb.sim.ticks import daily_ticks
    from orb.v3.data import V3Data

    ref = load_reference(cfg.reference)
    raw = Stores.raw_only(cfg.data.root)
    end = cfg.run.end_date
    idx = raw.read_daily(cfg.data.index.primary, cfg.data.daily_history_start, end)
    calendar = idx["date"].to_list()
    renames = ref.symbol_map.filter(pl.col("change_type") == "rename")
    daily = pl.concat(
        [bhav_for(s, raw, cfg.data.daily_history_start, end, renames) for s in ref.all_symbols()]
    )
    dq = Path(cfg.data.root) / "_dq" / "exclusions.parquet"
    if not dq.exists():
        raise SystemExit(f"{dq} missing: run `orb dq` first")
    excl = pl.read_parquet(dq).select("symbol", "date", "reason")
    whole = ref.day_exclusions().select(symbol=pl.lit("*"), date="date", reason="reason")

    def minute_loader(syms, a, b):
        parts = [raw.read_minute(s, a, b) for s in syms]
        return pl.concat(parts) if parts else pl.DataFrame()

    base = ContextBuilder(
        cfg,
        calendar,
        ref.membership,
        pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date}),
        pl.concat([excl, whole]),
        minute_loader,
        minute_loader,
        set(),
    )
    actions = csvio.read_csv(
        cfg.reference.path("corporate_actions"), try_parse_dates=True, infer_schema_length=0
    ).select(
        "symbol",
        pl.col("ex_date").str.to_date(strict=False),
        "action_type",
        pl.col("price_factor").cast(pl.Float64, strict=False),
        "subject",
    )
    ticks = {(r["symbol"], r["date"]): r["tick"] for r in daily_ticks(daily, cfg.ticks).to_dicts()}
    p = Path(cfg.circuit_guard.fno_membership)
    if not p.exists():
        raise SystemExit(f"{p} missing: run `orb ref fno` (cache only, no download)")
    fno = FnoCalendar(
        csvio.read_csv(p, try_parse_dates=True),
        ref.ban_list if cfg.circuit_guard.ban_list_implies_fno else None,
    )
    data = V3Data(cfg, base, daily, actions, ticks, fno, renames)
    return data, idx


def cmd_backtest_v3(cfg: ConfigV3, args: argparse.Namespace) -> None:
    from orb import provenance
    from orb.cli import prereg_config_hash
    from orb.repro import run_metadata
    from orb.reviews import require_reviewed
    from orb.v3.engine import EngineV3, write_run_v3

    require_reviewed(cfg.reference.root)
    prov = provenance.preflight(Path.cwd(), cfg.data.root, args.oos, strategy=STRATEGY)
    start = date.fromisoformat(args.start) if args.start else cfg.run.start_date
    end = date.fromisoformat(args.end) if args.end else cfg.run.oos_start - timedelta(days=1)
    prereg = prereg_config_hash(PREREG)
    if prereg != cfg.hash():
        log.warning(
            "config hash %s differs from %s (%s)",
            cfg.hash()[:12],
            PREREG,
            (prereg or "missing")[:12],
        )
        if args.oos:
            raise SystemExit("OOS refused: config differs from the pre-registered V3 config")
    data, idx = load_inputs(cfg)
    res = EngineV3(cfg, data, idx).run(start, end, oos=args.oos, force_reason=args.force_oos_reason)
    meta = {
        **run_metadata(cfg),
        "prereg_config_hash": prereg,
        "prereg_match": prereg == cfg.hash(),
        "provenance": {
            **{k: v for k, v in prov.items() if k != "will_pin"},
            "pinned_commit": prov["pinned_commit"] or prov["commit"],
            "pinned_by_this_run": prov["will_pin"],
        },
    }
    out_root = Path(args.out) / STRATEGY if args.out == "runs" else Path(args.out)
    out = write_run_v3(res, cfg, out_root, meta)
    if prov["will_pin"]:
        pin = provenance.save_pin(cfg.data.root, prov["commit"], out.name, strategy=STRATEGY)
        log.info("pinned V3 in-sample commit %s (run %s)", pin["commit"][:12], pin["run_id"])
    print(res.header())
    print(f"run written to {out}")


def cmd_report_v3(cfg: ConfigV3, args: argparse.Namespace) -> None:
    from orb.reports import drift_days_from_disk, tags_from_disk
    from orb.v3.report import build_report_v3

    out = build_report_v3(args.run_dir, cfg, tags_from_disk(cfg), drift_days_from_disk(cfg))
    print((out / "report.md").read_text().splitlines()[0])
    print(f"report written to {out}")
