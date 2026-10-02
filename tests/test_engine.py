from datetime import date

import polars as pl
import pytest

from orb.engine import Engine, OOSLockError, summarize, write_run
from orb.portfolio import TAKEN
from orb.scoring import RuleScorer
from orb.sim.ticks import daily_ticks
from tests.test_signals import golden_market


def _engine(m):
    ticks = {
        (r["symbol"], r["date"]): r["tick"] for r in daily_ticks(m.daily, m.cfg.ticks).to_dicts()
    }
    return Engine(m.cfg, m.builder(), ticks, RuleScorer(m.cfg.scoring))


def test_golden_day_end_to_end(cfg, tmp_path):
    m, T = golden_market(cfg)
    res = _engine(m).run(T, T, ledger=tmp_path / "l.jsonl")
    assert res.signals.height == 1
    # 1 signal x 2 variants x 3 slippage multipliers
    assert res.sims.height == 6
    assert set(res.book["book_decision"]) == {TAKEN}
    s = summarize(res.book)
    assert s.height == 6 and set(s["trades"]) == {1}
    # more slippage -> lower net
    d = s.filter(pl.col("variant") == "default").sort("slippage_mult")["net_pnl"].to_list()
    assert d[0] > d[1] > d[2]
    row = res.book.filter((pl.col("variant") == "default") & (pl.col("slippage_mult") == 1.0))
    r = row.row(0, named=True)
    assert r["costs_rounded"] is not None and r["net_pnl_rounded"] is not None
    assert res.header().startswith("survivorship gap: 0.00%")
    out = write_run(res, cfg, tmp_path / "runs", {"note": "test"})
    assert (out / "summary.txt").read_text().startswith("survivorship gap:")
    assert {"signals.parquet", "sims.parquet", "book.parquet", "meta.json"} <= {
        p.name for p in out.iterdir()
    }


def test_oos_locked_and_run_once(cfg, tmp_path):
    m, T = golden_market(cfg)
    e = _engine(m)
    ledger = tmp_path / "oos.jsonl"
    oos_cfg = cfg.model_copy(
        update={"run": cfg.run.model_copy(update={"oos_start": date(2024, 6, 1)})}
    )
    e.cfg = oos_cfg
    with pytest.raises(OOSLockError, match="locked OOS"):
        e.run(T, T, ledger=ledger)
    e.run(T, T, oos=True, ledger=ledger)
    assert ledger.read_text().count("\n") == 1
    with pytest.raises(OOSLockError, match="already run"):
        e.run(T, T, oos=True, ledger=ledger)
    e.run(T, T, oos=True, force_reason="data fix after bhavcopy revision", ledger=ledger)
    assert "data fix" in ledger.read_text()


def test_survivorship_gap_counts_members_without_data(cfg, tmp_path):
    m, T = golden_market(cfg)
    m.minute = pl.concat([m.minute])  # AAA only; add a member with no data
    mem = m.membership()
    m.membership = lambda: pl.concat([mem, mem.with_columns(symbol=pl.lit("ZZZ"))])
    res = _engine(m).run(T, T, ledger=tmp_path / "l.jsonl")
    g = res.survivorship_gap().filter(pl.col("year") == "ALL").row(0, named=True)
    assert (g["eligible_days"], g["missing_days"], g["gap_pct"]) == (2, 1, 50.0)
    assert "50.00%" in res.header()
