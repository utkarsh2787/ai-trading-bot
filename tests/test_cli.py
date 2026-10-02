import shutil
from datetime import date

import polars as pl
import pytest
import yaml

from orb.cli import main
from orb.data.store import ParquetStore
from orb.synthetic import daily_bars, minute_session
from tests.conftest import ROOT

DAYS = [date(2018, 1, d) for d in (1, 2, 3)]


def test_local_download_then_dq(tmp_path, capsys, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for n in ("default.yaml", "tick_sizes.yaml", "costs.yaml"):
        shutil.copy(ROOT / "config" / n, cfg_dir / n)
    raw = yaml.safe_load((cfg_dir / "default.yaml").read_text())
    raw["data"].update(
        provider="local", root=str(tmp_path / "data"), minute_history_start="2017-12-01"
    )
    raw["data"]["local"].update(
        root=str(tmp_path / "vendor"),
        columns={k: k for k in ("ts", "date", "open", "high", "low", "close", "volume")},
    )
    raw["reference"]["root"] = str(tmp_path / "ref")
    (cfg_dir / "default.yaml").write_text(yaml.safe_dump(raw))

    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "nifty200_membership.csv").write_text("date,symbol\n2018-01-01,AAA\n")
    (ref / "fo_ban.csv").write_text("date,symbol\n")
    (ref / "corporate_actions.csv").write_text("symbol,ex_date,action_type,price_factor\n")
    (ref / "special_sessions.csv").write_text("date,session_type\n")

    vendor = tmp_path / "vendor"
    (vendor / "minute").mkdir(parents=True)
    (vendor / "daily").mkdir()
    for sym in ("AAA", "NIFTY 200", "NIFTY 50", "INDIA VIX"):
        m = pl.concat([minute_session(sym, d, seed=i) for i, d in enumerate(DAYS)])
        if sym == "AAA":
            m = pl.concat([m, m.head(1)])  # inject a duplicate timestamp
        m.with_columns(pl.col("ts").dt.replace_time_zone(None)).drop("symbol").write_csv(
            vendor / "minute" / f"{sym}.csv"
        )
        # daily bars: random history before the test days, then the test days'
        # official bars aggregated from the same 1-min bars (as in real data)
        hist = daily_bars(sym, date(2016, 1, 1), 600).filter(pl.col("date") < DAYS[0])
        agg = (
            m.unique(subset=["ts"])
            .group_by(date=pl.col("ts").dt.date())
            .agg(
                open=pl.col("open").first(),
                high=pl.col("high").max(),
                low=pl.col("low").min(),
                close=pl.col("close").last(),
                volume=pl.col("volume").sum(),
            )
            .with_columns(symbol=pl.lit(sym))
        )
        pl.concat([hist, agg.select(hist.columns)]).sort("date").drop("symbol").write_parquet(
            vendor / "daily" / f"{sym}.parquet"
        )

    cfg_path = str(cfg_dir / "default.yaml")
    main(["--config", cfg_path, "download"])
    from orb.data import snapshot

    snap = snapshot.latest(tmp_path / "data", "local", frozen=False)
    vend = snap.store
    assert vend.symbols("minute") == ["AAA", "INDIA VIX", "NIFTY 200", "NIFTY 50"]
    assert vend.read_minute("AAA", DAYS[0], DAYS[-1]).height == 3 * 375  # dup collapsed

    # stock daily bars come from the bhavcopy (`orb ref bhavcopy`); simulate it
    raw = ParquetStore(tmp_path / "data" / "raw")
    raw.write_daily(vend.read_daily("AAA", date(2016, 1, 1), DAYS[-1]))
    with pytest.raises(snapshot.SnapshotError, match="frozen"):
        main(["--config", cfg_path, "build-raw"])  # unfrozen snapshots cannot be built
    main(["--config", cfg_path, "snapshot", "freeze"])
    with pytest.raises(snapshot.SnapshotError, match="frozen"):
        main(["--config", cfg_path, "download", "--snapshot", snap.id])
    main(["--config", cfg_path, "build-raw"])
    from orb.data.pipeline import read_build_record

    assert read_build_record(tmp_path / "data" / "raw")["snapshot_id"] == snap.id
    assert raw.symbols("minute") == ["AAA", "INDIA VIX", "NIFTY 200", "NIFTY 50"]
    assert raw.read_daily("NIFTY 200", DAYS[0], DAYS[-1]).height == 3  # index daily copied

    # inject a duplicate timestamp at the raw-store level to exercise dq
    f = raw.minute_dir("AAA") / "2018.parquet"
    m = pl.read_parquet(f)
    pl.concat([m, m.head(1)]).write_parquet(f)

    main(["--config", cfg_path, "dq"])
    dq = tmp_path / "data" / "_dq"
    excluded = pl.read_parquet(dq / "excluded_stock_days.parquet")
    assert excluded.to_dicts() == [{"symbol": "AAA", "date": DAYS[0]}]
    assert "duplicate_timestamp" in capsys.readouterr().out
    by_reason = pl.read_csv(dq / "exclusions_by_reason.csv")
    row = by_reason.filter(pl.col("reason") == "DUPLICATE_TIMESTAMP").row(0, named=True)
    assert row["stock_days"] == 1 and row["universe_days"] == 3
    assert (dq / "exclusions_by_year.csv").exists()
    assert (dq / "exclusions_by_vix_tercile.csv").exists()
    gap = pl.read_csv(dq / "survivorship_gap.csv").filter(pl.col("year") == "ALL")
    assert gap["gap_pct"][0] == 0.0  # AAA has minute data on all 3 member days

    main(["--config", cfg_path, "scan", "--start", "2018-01-01", "--end", "2018-01-03"])
    cands = pl.read_parquet(tmp_path / "data" / "_scan" / "candidates.parquet")
    # only 3 minute sessions exist: nothing can be traded. The duplicate-timestamp day
    # is still logged, as EXCLUDED_DQ; the rest are rejected for history.
    assert set(cands["decision"].to_list()) <= {"REJECTED", "EXCLUDED"}
    for r in cands.to_dicts():
        if r["decision"] == "EXCLUDED":
            assert r["date"] == DAYS[0] and r["reason"] == "EXCLUDED_DQ"
        else:
            assert r["reason"].startswith("INSUFFICIENT_HISTORY")

    # backtests need a clean git tree: run from an empty committed repo
    from tests.test_provenance import make_repo

    monkeypatch.chdir(make_repo(tmp_path / "repo"))
    main(
        [
            "--config",
            cfg_path,
            "backtest",
            "--start",
            "2018-01-01",
            "--end",
            "2018-01-03",
            "--out",
            str(tmp_path / "runs"),
        ]
    )
    run = next((tmp_path / "runs").iterdir())
    meta = __import__("json").loads((run / "meta.json").read_text())
    assert meta["vendor_snapshot_id"] == snap.id and meta["config_hash"]
    assert meta["survivorship_gap"].startswith("survivorship gap: 0.00%")
    assert (run / "summary.txt").read_text().startswith("survivorship gap:")

    main(["--config", cfg_path, "report", str(run)])
    md = (run / "report" / "report.md").read_text()
    assert md.splitlines()[0].startswith("survivorship gap:")


def test_prereg_hash_is_read_from_the_document():
    from orb.cli import prereg_config_hash
    from orb.config import load_config

    assert (
        prereg_config_hash(ROOT / "docs" / "PREREGISTRATION.md")
        == load_config(ROOT / "config" / "default.yaml").hash()
    )
