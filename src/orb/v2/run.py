"""``orb backtest --strategy v2`` / ``orb report --strategy v2``.

Inputs are the same files V1 used: the raw store, reference files and the DQ
exclusions in ``data/_dq`` (``scan.load_from_disk``), plus the F&O membership
built from cached F&O bhavcopies (``orb ref fno``) and the index daily context.
The gate: review rows, clean tree, logged changes since the V2 pin, config hash
vs docs/PREREGISTRATION_V2.md; the first in-sample run pins the V2 commit.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from orb import csvio
from orb.v2.config import ConfigV2

log = logging.getLogger("orb")

STRATEGY = "v2"
PREREG = "docs/PREREGISTRATION_V2.md"


def load_inputs(cfg: ConfigV2):
    from orb.data.pipeline import Stores
    from orb.data.reference import load_reference
    from orb.features import daily_context
    from orb.refdata.fno import FnoCalendar
    from orb.scan import load_from_disk
    from orb.sim.ticks import daily_ticks
    from orb.v2.context import V2ContextBuilder

    base, daily = load_from_disk(cfg)
    builder = V2ContextBuilder.from_builder(base)
    ticks = {(r["symbol"], r["date"]): r["tick"] for r in daily_ticks(daily, cfg.ticks).to_dicts()}
    p = Path(cfg.circuit_guard.fno_membership)
    if not p.exists():
        raise SystemExit(f"{p} missing: run `orb ref fno` (cache only, no download)")
    samples = csvio.read_csv(p, try_parse_dates=True)
    ref = load_reference(cfg.reference)
    fno = FnoCalendar(samples, ref.ban_list if cfg.circuit_guard.ban_list_implies_fno else None)
    raw = Stores.raw_only(cfg.data.root)
    names = [cfg.data.index.primary, cfg.data.index.fallback]
    idx_daily = pl.concat(
        [raw.read_daily(n, cfg.data.daily_history_start, cfg.run.end_date) for n in names]
    )
    empty_actions = pl.DataFrame(
        schema={
            "symbol": pl.String,
            "ex_date": pl.Date,
            "action_type": pl.String,
            "price_factor": pl.Float64,
        }
    )
    no_invalid = pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date})
    ictx = daily_context(idx_daily, empty_actions, no_invalid, builder.calendar, cfg.features)
    index_ctx = {(r["symbol"], r["date"]): r for r in ictx.to_dicts()}
    return builder, ticks, fno, index_ctx


def cmd_backtest_v2(cfg: ConfigV2, args: argparse.Namespace) -> None:
    from orb import provenance
    from orb.cli import prereg_config_hash
    from orb.repro import run_metadata
    from orb.reviews import require_reviewed
    from orb.v2.engine import EngineV2, summarize_v2, write_run_v2

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
            raise SystemExit("OOS refused: config differs from the pre-registered V2 config")
    builder, ticks, fno, index_ctx = load_inputs(cfg)
    res = EngineV2(cfg, builder, ticks, fno, index_ctx).run(
        start, end, oos=args.oos, force_reason=args.force_oos_reason
    )
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
    out = write_run_v2(res, cfg, out_root, meta)
    if prov["will_pin"]:
        pin = provenance.save_pin(cfg.data.root, prov["commit"], out.name, strategy=STRATEGY)
        log.info("pinned V2 in-sample commit %s (run %s)", pin["commit"][:12], pin["run_id"])
    print(res.header())
    print(summarize_v2(res.book))
    print(f"run written to {out}")


def cmd_report_v2(cfg: ConfigV2, args: argparse.Namespace) -> None:
    from orb.reports import drift_days_from_disk, tags_from_disk
    from orb.v2.report import build_report_v2

    out = build_report_v2(args.run_dir, cfg, tags_from_disk(cfg), drift_days_from_disk(cfg))
    print((out / "report.md").read_text().splitlines()[0])
    print(f"report written to {out}")
