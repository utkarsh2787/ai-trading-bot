"""Command line: ``orb download`` and ``orb dq`` (backtest commands come later)."""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

import polars as pl

from orb.config import Config, load_config
from orb.data.download import Downloader, Manifest
from orb.data.provider import DataProvider
from orb.data.quality import (
    check_daily,
    check_minute,
    concat_issues,
    excluded_stock_days,
    reconcile_daily_minute,
)
from orb.data.reference import load_reference
from orb.data.store import ParquetStore

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


def cmd_download(cfg: Config, args: argparse.Namespace) -> None:
    ref = load_reference(cfg.reference)
    symbols = args.symbols or (ref.all_symbols() + index_symbols(cfg))
    store = ParquetStore(cfg.data.root)
    manifest = Manifest(Path(cfg.data.root) / "_manifest" / "downloads.jsonl")
    dl = Downloader(
        make_provider(cfg),
        store,
        manifest,
        {"minute": cfg.data.kite.minute_chunk_days, "daily": cfg.data.kite.day_chunk_days},
    )
    today = date.today()
    end = min(cfg.run.end_date, today)
    for kind, start in (
        ("daily", cfg.data.daily_history_start),
        ("minute", cfg.data.minute_history_start),
    ):
        if args.kind in (kind, "all"):
            r = dl.run(symbols, kind, start, end, today)  # type: ignore[arg-type]
            log.info(
                "%s: fetched=%d skipped=%d unresolved=%d failed=%d",
                kind,
                len(r.fetched),
                r.skipped,
                len(r.unresolved),
                len(r.failed),
            )
            if r.unresolved:
                log.warning(
                    "unresolved (survivorship risk; need vendor data): %s",
                    ", ".join(sorted(set(r.unresolved))),
                )


def cmd_dq(cfg: Config, args: argparse.Namespace) -> None:
    ref = load_reference(cfg.reference)
    store = ParquetStore(cfg.data.root)
    special = set(ref.special_sessions["date"].to_list())
    indices = set(index_symbols(cfg))
    start, end = cfg.data.minute_history_start, cfg.run.end_date
    parts = []
    for symbol in args.symbols or store.symbols("minute"):
        is_index = symbol in indices
        minute = store.read_minute(symbol, start, end)
        daily = store.read_daily(symbol, cfg.data.daily_history_start, end)
        parts += [
            check_minute(minute, cfg.dq, cfg.session, is_index, special),
            check_daily(daily, ref.corporate_actions, cfg.dq, is_index),
            reconcile_daily_minute(daily, minute, cfg.dq),
        ]
    issues = concat_issues(parts)
    out = Path(cfg.data.root) / "_dq"
    out.mkdir(parents=True, exist_ok=True)
    issues.write_parquet(out / "issues.parquet")
    excluded_stock_days(issues).write_parquet(out / "excluded_stock_days.parquet")
    summary = issues.group_by("check", "severity").agg(n=pl.len()).sort("severity", "check")
    print(summary)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="orb")
    p.add_argument("--config", default="config/default.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download", help="fetch and cache bars (resumable)")
    d.add_argument("--kind", choices=["daily", "minute", "all"], default="all")
    d.add_argument("--symbols", nargs="*")
    q = sub.add_parser("dq", help="run data-quality checks on the cache")
    q.add_argument("--symbols", nargs="*")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    {"download": cmd_download, "dq": cmd_dq}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
