"""Command line.

orb ref {symbols,nifty200,bhavcopy,ca,ban,all}   reference data from NSE / niftyindices
orb download                                     vendor bars -> vendor/<provider>/snapshots/<id>
orb snapshot {list,freeze,verify}                freeze = immutable + hashed
orb build-raw                                    vendor -> data/raw (de-adjusted)
orb dq                                           checks + exclusion report on data/raw
orb scan                                         first-breakout candidates (OOS needs --oos)
"""

from __future__ import annotations

import argparse
import logging
import shutil
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from orb import csvio
from orb.config import Config, load_config
from orb.data import snapshot
from orb.data.download import Downloader, Manifest
from orb.data.exclusions import (
    exclusion_report,
    exclusion_table,
    survivorship_gap,
    survivorship_header,
    universe_days,
)
from orb.data.pipeline import (
    BuildReport,
    Stores,
    bhav_for,
    build_raw_symbol,
    write_build_record,
)
from orb.data.provider import DataProvider
from orb.data.quality import (
    check_daily,
    check_minute,
    check_volume,
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

        return KiteProvider(client_from_env(cfg.data.kite, cfg.data.root), cfg.data.kite)
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
    raw = Stores.raw_only(cfg.data.root)
    start, end = cfg.data.daily_history_start, _end(cfg)
    steps = (
        ["symbols", "nifty200", "bhavcopy", "ca", "ban", "expiries", "results"]
        if args.what == "all"
        else [args.what]
    )
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
        elif step == "mergers":
            b.merger_candidates(raw)
        elif step == "sessions":
            snaps = snapshot.list_snapshots(cfg.data.root, cfg.data.provider)
            minute_store = raw if raw.symbols("minute") else (snaps[-1].store if snaps else None)
            b.special_sessions(raw, minute_store)
        elif step == "expiries":
            b.expiries(cfg.data.minute_history_start, end)
        elif step == "results":
            b.results_dates(cfg.data.minute_history_start, end)
        elif step == "fno":
            from orb.refdata.fno import samples_from_cache

            ref_root = Path(cfg.reference.root)
            sm = csvio.read_csv(ref_root / cfg.reference.symbol_map, try_parse_dates=True)
            fno = samples_from_cache(
                ref_root / "_cache" / "fo_bhavcopy", sm.filter(pl.col("change_type") == "rename")
            )
            csvio.write_csv(fno, ref_root / "fno_stocks.csv")
            log.info(
                "fno: %d weekly samples, %d symbols -> %s (cache only, no download)",
                fno["sample_date"].n_unique(),
                fno["symbol"].n_unique(),
                ref_root / "fno_stocks.csv",
            )


# ----------------------------------------------------------------- download


def cmd_login(cfg: Config, args: argparse.Namespace) -> None:
    from orb.data import kite_auth

    if args.request_token:
        tok = kite_auth.extract_request_token(args.request_token)
    else:
        print("1. Open this URL, log in to Kite, and approve the app:\n")
        print("   " + kite_auth.login_url() + "\n")
        print("2. You are redirected to your app's redirect URL. Paste that full URL")
        print("   (or just the request_token value) here.\n")
        tok = kite_auth.extract_request_token(input("redirect URL or request_token: "))
    meta = kite_auth.create_session(tok, cfg.data.root)
    print(
        f"logged in as {meta['user_id']}; session valid until {meta['expires_after']} "
        f"(saved to {kite_auth.session_path(cfg.data.root)})"
    )


def cmd_download(cfg: Config, args: argparse.Namespace) -> None:
    from orb.data import download_plan
    from orb.data.reference import load_table

    # only the symbol map is needed; other reference files (ban list, sessions...)
    # must not block the download
    symbol_map = load_table("symbol_map", cfg.reference.path("symbol_map"), required=False)
    if args.symbols:
        symbols = args.symbols
    else:
        symbols, counts = download_plan.download_symbols(cfg)
        log.info("download list: %s", ", ".join(f"{k}={v}" for k, v in counts.items()))
    if args.plan:  # no Kite calls, no snapshot created
        open_snap = snapshot.latest(cfg.data.root, cfg.data.provider, frozen=False)
        man = Manifest(open_snap.manifest_path) if open_snap else None
        p = download_plan.plan(cfg, symbols, _end(cfg), man, cfg.data.provider)
        print(p.text(cfg.data.kite.max_requests_per_sec))
        if open_snap:
            print(f"resumes open snapshot {open_snap.id}")
        return
    snap = snapshot.for_download(cfg.data.root, cfg.data.provider, args.snapshot)
    log.info("downloading into snapshot %s (freeze it with `orb snapshot freeze`)", snap.id)
    dl = Downloader(
        make_provider(cfg),
        snap.store,
        Manifest(snap.manifest_path),
        {"minute": cfg.data.kite.minute_chunk_days, "daily": cfg.data.kite.day_chunk_days},
        resolver=SymbolResolver(symbol_map),
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
    cmd_coverage(cfg, argparse.Namespace(snapshot=snap.id, symbols=symbols))


def cmd_coverage(cfg: Config, args: argparse.Namespace) -> None:
    """Per-symbol first/last 1-min date and the >= 95% coverage date."""
    from orb.data import download_plan

    snap = (
        snapshot.Snapshot(snapshot.snapshots_dir(cfg.data.root, cfg.data.provider) / args.snapshot)
        if args.snapshot
        else snapshot.list_snapshots(cfg.data.root, cfg.data.provider)[-1]
    )
    symbols = args.symbols or download_plan.download_symbols(cfg)[0]
    cov, line = download_plan.coverage(snap.store, symbols)
    out = Path(cfg.data.root) / "_manifest" / f"coverage_{snap.id}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    csvio.write_csv(cov, out)
    print(cov)
    print(line)
    print(f"per-symbol coverage written to {out}")


# ---------------------------------------------------------------- build-raw


def cmd_snapshot(cfg: Config, args: argparse.Namespace) -> None:
    root, prov = cfg.data.root, cfg.data.provider
    if args.action == "list":
        for s in snapshot.list_snapshots(root, prov):
            print(s.id, "frozen" if s.frozen else "open", s.content_hash if s.frozen else "")
    elif args.action == "freeze":
        s = (
            snapshot.Snapshot(snapshot.snapshots_dir(root, prov) / args.id)
            if args.id
            else snapshot.latest(root, prov, frozen=False)
        )
        if s is None:
            raise SystemExit("no open snapshot to freeze")
        meta = s.freeze(prov)
        print(f"froze {s.id}: {meta['n_files']} files, content hash {meta['content_hash']}")
    elif args.action == "verify":
        s = snapshot.for_build(root, prov, args.id)  # raises on mismatch
        print(f"{s.id} OK ({s.content_hash})")


def cmd_build_raw(cfg: Config, args: argparse.Namespace) -> None:
    snap = snapshot.for_build(cfg.data.root, cfg.data.provider, args.snapshot)
    stores = Stores(vendor=snap.store, raw=Stores.raw_only(cfg.data.root))
    ref = load_reference(cfg.reference)
    indices = set(index_symbols(cfg))
    report = BuildReport()
    renames = ref.symbol_map.filter(pl.col("change_type") == "rename")
    if not args.symbols:  # full rebuild from this snapshot: drop minute bars of older builds
        shutil.rmtree(Path(cfg.data.root) / "raw" / "minute", ignore_errors=True)
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
            ref.corporate_actions,
            cfg.data.deadjust_drift_tolerance,
            renames,
        )
    issues = report.issue_frame()
    out = Path(cfg.data.root) / "_dq"
    out.mkdir(parents=True, exist_ok=True)
    issues.write_parquet(out / "deadjust_issues.parquet")
    csvio.write_csv(report.volume_fix_frame(), out / "volume_corrections.csv")
    drift = report.drift_frame()
    csvio.write_csv(drift, out / "factor_drift.csv")
    if drift.height:
        log.warning(
            "factor drift: %d runs on %d symbols (see %s)",
            drift.height,
            drift["symbol"].n_unique(),
            out / "factor_drift.csv",
        )
    write_build_record(Path(cfg.data.root) / "raw", snap.id, snap.content_hash)
    log.info(
        "build-raw: %d symbols, %d minute rows, %d de-adjust issues",
        report.symbols,
        report.minute_rows,
        issues.height,
    )


# ----------------------------------------------------------------------- dq


def cmd_dq(cfg: Config, args: argparse.Namespace) -> None:
    # reference CSVs first: a ragged row (e.g. an unquoted comma) must stop dq
    # before anything is parsed with shifted columns
    ragged = csvio.check_tree(cfg.reference.root)
    out = Path(cfg.data.root) / "_dq"
    csvio.write_csv(ragged, out / "ref_csv_issues.csv")
    if ragged.height:
        lines = [
            f"  {r['file']}:{r['line']}: {r['fields']} fields, header has {r['expected']}"
            for r in ragged.to_dicts()
        ]
        raise SystemExit(
            "dq refused: ragged rows in reference CSVs (quote fields that "
            "contain commas):\n" + "\n".join(lines)
        )
    log.info("reference CSVs: no ragged rows")
    vfix = Path(cfg.data.root) / "_dq" / "volume_corrections.csv"
    if vfix.exists():
        vf = csvio.read_csv(vfix)
        log.info(
            "split/bonus volume fix (build-raw): %d stock-days corrected on %d symbols",
            vf.height,
            vf["symbol"].n_unique() if vf.height else 0,
        )
    ref = load_reference(cfg.reference)
    raw = Stores.raw_only(cfg.data.root)
    special = set(ref.special_sessions["date"].to_list())
    indices = set(index_symbols(cfg))
    start, end = cfg.data.minute_history_start, cfg.run.end_date
    renames = ref.symbol_map.filter(pl.col("change_type") == "rename")
    parts, daily_parts = [], []
    symbols = args.symbols or sorted(set(raw.symbols("minute")) | set(raw.symbols("daily")))
    for symbol in symbols:
        is_index = symbol in indices
        minute = raw.read_minute(symbol, start, end)
        daily = (  # stocks: official daily bars across renames
            raw.read_daily(symbol, cfg.data.daily_history_start, end)
            if is_index
            else bhav_for(symbol, raw, cfg.data.daily_history_start, end, renames)
        )
        daily_parts.append(check_daily(daily, ref.corporate_actions, cfg.dq, is_index))
        parts += [
            check_minute(minute, cfg.dq, cfg.session, is_index, special),
            reconcile_daily_minute(daily, minute, cfg.dq),
            *([] if is_index else [check_volume(daily, minute)]),
        ]
    out = Path(cfg.data.root) / "_dq"
    out.mkdir(parents=True, exist_ok=True)
    deadjust = out / "deadjust_issues.parquet"
    if deadjust.exists():
        parts.append(pl.read_parquet(deadjust))
    daily_issues = concat_issues(daily_parts)
    daily_issues.write_parquet(out / "daily_issues.parquet")  # ATR validity uses these
    issues = concat_issues([*parts, daily_issues])
    issues.write_parquet(out / "issues.parquet")
    excluded_stock_days(issues).write_parquet(out / "excluded_stock_days.parquet")
    print(issues.group_by("check", "severity").agg(n=pl.len()).sort("severity", "check"))

    # exclusion report over point-in-time universe stock-days
    whole_market = set(ref.day_exclusions()["date"].to_list())  # sessions/outages: not eligible
    days = [
        d
        for d in raw.read_daily(cfg.data.index.primary, start, end)["date"].to_list()
        if d not in whole_market
    ]
    universe = universe_days(ref.membership, days)
    excl = exclusion_table(issues, ref, cfg.reference.excluding_action_types, universe)
    vix = vix_terciles(
        raw.read_daily(cfg.data.index.vix, cfg.data.daily_history_start, end),
        cfg.validation.vix_min_history,
    )
    excl.write_parquet(out / "exclusions.parquet")
    minute_days = pl.DataFrame(
        [(s, d) for s in raw.symbols("minute") for d in raw.minute_dates(s)],
        schema={"symbol": pl.String, "date": pl.Date},
        orient="row",
    )
    gap = survivorship_gap(universe, minute_days)
    csvio.write_csv(gap, out / "survivorship_gap.csv")
    print(f"\n{survivorship_header(gap)}\n{gap}")
    for name, table in exclusion_report(excl, universe, vix).items():
        csvio.write_csv(table, out / f"exclusions_{name}.csv")
        print(f"\nexcluded stock-days {name}:\n{table}")


# ----------------------------------------------------------------- backtest


def cmd_backtest(cfg: Config, args: argparse.Namespace) -> None:
    from orb import provenance
    from orb.engine import Engine, summarize, write_run
    from orb.repro import run_metadata
    from orb.reviews import require_reviewed
    from orb.scan import load_from_disk
    from orb.scoring import RuleScorer
    from orb.sim.ticks import daily_ticks

    require_reviewed(cfg.reference.root)  # no run while any manual row is unreviewed
    prov = provenance.preflight(Path.cwd(), cfg.data.root, args.oos)  # clean tree, logged changes
    start = date.fromisoformat(args.start) if args.start else cfg.run.start_date
    end = date.fromisoformat(args.end) if args.end else cfg.run.oos_start - timedelta(days=1)
    prereg = prereg_config_hash()
    if prereg != cfg.hash():
        log.warning(
            "config hash %s differs from docs/PREREGISTRATION.md (%s)",
            cfg.hash()[:12],
            (prereg or "missing")[:12],
        )
        if args.oos:
            raise SystemExit("OOS refused: config differs from the pre-registered config")
    builder, daily = load_from_disk(cfg)
    ticks = {(r["symbol"], r["date"]): r["tick"] for r in daily_ticks(daily, cfg.ticks).to_dicts()}
    res = Engine(cfg, builder, ticks, RuleScorer(cfg.scoring)).run(
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
    out = write_run(res, cfg, args.out, meta)
    if prov["will_pin"]:  # first successful in-sample run pins the code
        pin = provenance.save_pin(cfg.data.root, prov["commit"], out.name)
        log.info("pinned in-sample commit %s (run %s)", pin["commit"][:12], pin["run_id"])
    print(res.header())
    print(summarize(res.book))
    print(f"run written to {out}")


def prereg_config_hash(path: str | Path = "docs/PREREGISTRATION.md") -> str | None:
    import re

    p = Path(path)
    m = re.search(r"Config hash \| `([0-9a-f]{64})`", p.read_text()) if p.exists() else None
    return m.group(1) if m else None


def cmd_report(cfg: Config, args: argparse.Namespace) -> None:
    from orb.reports import build_report, drift_days_from_disk, tags_from_disk

    out = build_report(
        args.run_dir,
        cfg,
        tags_from_disk(cfg),
        drift_days_from_disk(cfg),
        Path(cfg.data.root) / "_dq" / "factor_drift.csv",
    )
    print((out / "report.md").read_text().splitlines()[0])
    print(f"report written to {out}")


# --------------------------------------------------------------------- scan


def cmd_scan(cfg: Config, args: argparse.Namespace) -> None:
    from orb.scan import builder_from_disk, scan_range
    from orb.scoring import RuleScorer

    start = date.fromisoformat(args.start) if args.start else cfg.run.start_date
    end = date.fromisoformat(args.end) if args.end else cfg.run.end_date
    if end >= cfg.run.oos_start and not args.oos:
        end = cfg.run.oos_start - timedelta(days=1)
        log.info("scan capped at %s: the OOS period needs --oos", end)
    cands = scan_range(builder_from_disk(cfg), cfg, RuleScorer(cfg.scoring), start, end)
    out = Path(cfg.data.root) / "_scan"
    out.mkdir(parents=True, exist_ok=True)
    cands.write_parquet(out / "candidates.parquet")
    print(cands.group_by("decision", "reason").agg(n=pl.len()).sort("n", descending=True))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="orb")
    p.add_argument("--config", help="default: config/default.yaml (v1), config/v2.yaml (v2)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("ref", help="download/parse reference data from NSE public sources")
    r.add_argument(
        "what",
        choices=[
            "sessions",
            "symbols",
            "nifty200",
            "bhavcopy",
            "ca",
            "ban",
            "expiries",
            "results",
            "mergers",
            "fno",
            "all",
        ],
    )
    d = sub.add_parser("download", help="fetch and cache vendor bars (resumable)")
    d.add_argument("--kind", choices=["daily", "minute", "all"], default="all")
    d.add_argument("--symbols", nargs="*")
    d.add_argument("--snapshot", help="resume this open snapshot (default: latest open/new)")
    d.add_argument(
        "--plan",
        action="store_true",
        help="print symbols, ranges, requests and runtime; no Kite calls",
    )
    lg = sub.add_parser("login", help="Kite Connect login: saves today's access token")
    lg.add_argument("--request-token", help="redirect URL or request_token (else prompts)")
    cv = sub.add_parser("coverage", help="1-min coverage of a vendor snapshot")
    cv.add_argument("--snapshot")
    cv.add_argument("--symbols", nargs="*")
    sn = sub.add_parser("snapshot", help="list / freeze / verify vendor snapshots")
    sn.add_argument("action", choices=["list", "freeze", "verify"])
    sn.add_argument("id", nargs="?")
    b = sub.add_parser("build-raw", help="frozen vendor snapshot -> raw store (de-adjusted)")
    b.add_argument("--symbols", nargs="*")
    b.add_argument("--snapshot", help="frozen snapshot id (default: latest frozen)")
    q = sub.add_parser("dq", help="data-quality checks and exclusion report on the raw store")
    q.add_argument("--symbols", nargs="*")
    s = sub.add_parser("scan", help="first-breakout candidates with features and scores")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--oos", action="store_true", help="allow dates in the locked OOS period")
    bt = sub.add_parser("backtest", help="run the engine (in-sample unless --oos)")
    bt.add_argument("--start")
    bt.add_argument("--end")
    bt.add_argument("--out", default="runs")
    bt.add_argument("--oos", action="store_true", help="run the locked OOS period (once)")
    bt.add_argument("--force-oos-reason", help="rerun OOS anyway; the reason is logged")
    bt.add_argument("--strategy", choices=["v1", "v2"], default="v1")
    rp = sub.add_parser("report", help="build the report for a run folder")
    rp.add_argument("run_dir")
    rp.add_argument("--strategy", choices=["v1", "v2"], default="v1")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from orb.data.kite_auth import load_dotenv

    load_dotenv(".env")
    if getattr(args, "strategy", "v1") == "v2":
        from orb.v2.config import load_config_v2
        from orb.v2.run import cmd_backtest_v2, cmd_report_v2

        cfg2 = load_config_v2(args.config or "config/v2.yaml")
        {"backtest": cmd_backtest_v2, "report": cmd_report_v2}[args.cmd](cfg2, args)
        return
    cfg = load_config(args.config or "config/default.yaml")
    {
        "ref": cmd_ref,
        "download": cmd_download,
        "login": cmd_login,
        "coverage": cmd_coverage,
        "snapshot": cmd_snapshot,
        "build-raw": cmd_build_raw,
        "dq": cmd_dq,
        "scan": cmd_scan,
        "backtest": cmd_backtest,
        "report": cmd_report,
    }[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
