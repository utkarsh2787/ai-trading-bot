"""Command line.

orb ref {symbols,nifty200,bhavcopy,ca,ban,all}   reference data from NSE / niftyindices
orb download                                     vendor bars -> data/vendor/<provider>
orb build-raw                                    vendor -> data/raw (de-adjusted)
orb dq                                           checks + exclusion report on data/raw
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

import polars as pl

from orb.config import Config, load_config
from orb.data.download import Downloader, Manifest
from orb.data.exclusions import exclusion_report, exclusion_table, universe_days
from orb.data.pipeline import BuildReport, Stores, build_raw_symbol
from orb.data.provider import DataProvider
from orb.data.quality import (
    check_daily,
    check_minute,
    concat_issues,
    excluded_stock_days,
    reconcile_daily_minute,
)
from orb.data.reference import SymbolResolver, load_reference
from orb.regimes import vix_terciles

log = logging.getLogger("orb")


def make_provider(cfg: Config) -> DataProvider:
    if cfg.data.provider == "kite":
        from orb.data.kite import KiteProvider, client_from_env

        return KiteProvider(client_from_env(cfg.data.kite), cfg.data.kite)
    from orb.data.local import LocalProvider

    return LocalProvider(cfg.data.local)


def index_symbols(cfg: Config) -> list[str]:
    i = cfg.data.index
    return [i.primary, i.fallback, i.vix]


def _end(cfg: Config) -> date:
    return min(cfg.run.end_date, date.today())


# ---------------------------------------------------------------------- ref


def cmd_ref(cfg: Config, args: argparse.Namespace) -> None:
    from orb.refdata.build import RefBuilder

    b = RefBuilder(cfg)
    raw = Stores.under(cfg.data.root, cfg.data.provider).raw
    start, end = cfg.data.daily_history_start, _end(cfg)
    steps = ["symbols", "nifty200", "bhavcopy", "ca", "ban"] if args.what == "all" else [args.what]
    for step in steps:
        log.info("ref: %s", step)
        if step == "symbols":
            b.symbols()
        elif step == "nifty200":
            b.nifty200(cfg.data.minute_history_start, date.today())
        elif step == "bhavcopy":
            b.bhavcopy(raw, start, end)
        elif step == "ca":
            b.corporate_actions(raw, start, end)
        elif step == "ban":
            b.ban_list(cfg.data.minute_history_start, end)


# ----------------------------------------------------------------- download


def cmd_download(cfg: Config, args: argparse.Namespace) -> None:
    ref = load_reference(cfg.reference)
    symbols = args.symbols or (ref.all_symbols() + index_symbols(cfg))
    stores = Stores.under(cfg.data.root, cfg.data.provider)
    manifest = Manifest(Path(cfg.data.root) / "_manifest" / "downloads.jsonl")
    dl = Downloader(
        make_provider(cfg),
        stores.vendor,
        manifest,
        {"minute": cfg.data.kite.minute_chunk_days, "daily": cfg.data.kite.day_chunk_days},
        resolver=SymbolResolver(ref.symbol_map),
    )
    today = date.today()
    for kind, start in (
        ("daily", cfg.data.daily_history_start),
        ("minute", cfg.data.minute_history_start),
    ):
        if args.kind in (kind, "all"):
            r = dl.run(symbols, kind, start, _end(cfg), today)  # type: ignore[arg-type]
            log.info(
                "%s: fetched=%d skipped=%d renamed=%d merged=%d delisted=%d failed=%d",
                kind,
                len(r.fetched),
                r.skipped,
                len(set(r.renamed)),
                len(set(r.merged)),
                len(set(r.delisted)),
                len(r.failed),
            )
            for old, new in sorted(set(r.renamed)):
                log.info("renamed: %s -> %s (fetched under the new symbol)", old, new)
            for old, new in sorted(set(r.merged)):
                log.warning("merged: %s into %s (history needs vendor data)", old, new)
            if r.delisted:
                log.warning(
                    "delisted (survivorship risk; need vendor data): %s",
                    ", ".join(sorted(set(r.delisted))),
                )


# ---------------------------------------------------------------- build-raw


def cmd_build_raw(cfg: Config, args: argparse.Namespace) -> None:
    stores = Stores.under(cfg.data.root, cfg.data.provider)
    indices = set(index_symbols(cfg))
    report = BuildReport()
    for symbol in args.symbols or stores.vendor.symbols("minute"):
        build_raw_symbol(
            symbol,
            stores,
            cfg.data.daily_history_start,
            _end(cfg),
            cfg.data.price_basis(),
            symbol in indices,
            cfg.data.deadjust_tolerance,
            report,
        )
    issues = report.issue_frame()
    out = Path(cfg.data.root) / "_dq"
    out.mkdir(parents=True, exist_ok=True)
    issues.write_parquet(out / "deadjust_issues.parquet")
    log.info(
        "build-raw: %d symbols, %d minute rows, %d de-adjust issues",
        report.symbols,
        report.minute_rows,
        issues.height,
    )


# ----------------------------------------------------------------------- dq


def cmd_dq(cfg: Config, args: argparse.Namespace) -> None:
    ref = load_reference(cfg.reference)
    raw = Stores.under(cfg.data.root, cfg.data.provider).raw
    special = set(ref.special_sessions["date"].to_list())
    indices = set(index_symbols(cfg))
    start, end = cfg.data.minute_history_start, cfg.run.end_date
    parts = []
    for symbol in args.symbols or raw.symbols("minute"):
        is_index = symbol in indices
        minute = raw.read_minute(symbol, start, end)
        daily = raw.read_daily(symbol, cfg.data.daily_history_start, end)
        parts += [
            check_minute(minute, cfg.dq, cfg.session, is_index, special),
            check_daily(daily, ref.corporate_actions, cfg.dq, is_index),
            reconcile_daily_minute(daily, minute, cfg.dq),
        ]
    out = Path(cfg.data.root) / "_dq"
    out.mkdir(parents=True, exist_ok=True)
    deadjust = out / "deadjust_issues.parquet"
    if deadjust.exists():
        parts.append(pl.read_parquet(deadjust))
    issues = concat_issues(parts)
    issues.write_parquet(out / "issues.parquet")
    excluded_stock_days(issues).write_parquet(out / "excluded_stock_days.parquet")
    print(issues.group_by("check", "severity").agg(n=pl.len()).sort("severity", "check"))

    # exclusion report over point-in-time universe stock-days
    days = raw.read_daily(cfg.data.index.primary, start, end)["date"].to_list()
    universe = universe_days(ref.membership, days)
    excl = exclusion_table(issues, ref, cfg.reference.excluding_action_types, universe)
    vix = vix_terciles(
        raw.read_daily(cfg.data.index.vix, cfg.data.daily_history_start, end),
        cfg.run.start_date,
        cfg.run.oos_start,
    )
    excl.write_parquet(out / "exclusions.parquet")
    for name, table in exclusion_report(excl, universe, vix).items():
        table.write_csv(out / f"exclusions_{name}.csv")
        print(f"\nexcluded stock-days {name}:\n{table}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="orb")
    p.add_argument("--config", default="config/default.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("ref", help="download/parse reference data from NSE public sources")
    r.add_argument("what", choices=["symbols", "nifty200", "bhavcopy", "ca", "ban", "all"])
    d = sub.add_parser("download", help="fetch and cache vendor bars (resumable)")
    d.add_argument("--kind", choices=["daily", "minute", "all"], default="all")
    d.add_argument("--symbols", nargs="*")
    b = sub.add_parser("build-raw", help="vendor store -> raw store (de-adjust if needed)")
    b.add_argument("--symbols", nargs="*")
    q = sub.add_parser("dq", help="data-quality checks and exclusion report on the raw store")
    q.add_argument("--symbols", nargs="*")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    {"ref": cmd_ref, "download": cmd_download, "build-raw": cmd_build_raw, "dq": cmd_dq}[args.cmd](
        cfg, args
    )


if __name__ == "__main__":
    main()
