"""Deliverable 5 on the hand-computed golden day."""

import polars as pl
import pytest

from orb import labels as lab
from orb.engine import Engine, write_run
from orb.scoring import RuleScorer
from orb.sim.ticks import daily_ticks
from tests.test_engine_golden import EXPECTED, golden_day


@pytest.fixture(scope="module")
def res(cfg, tmp_path_factory):
    m, T = golden_day(cfg)
    ticks = {
        (r["symbol"], r["date"]): r["tick"] for r in daily_ticks(m.daily, cfg.ticks).to_dicts()
    }
    return Engine(cfg, m.builder(), ticks, RuleScorer(cfg.scoring)).run(
        T, T, ledger=tmp_path_factory.mktemp("l") / "l.jsonl"
    )


def test_signal_log_has_every_first_breakout_with_spec_columns(res):
    log = lab.signal_log(res.signals, res.book)
    assert log.columns[:29] == lab.SIGNAL_LOG_COLUMNS[:29]
    rows = {r["stock"]: r for r in log.to_dicts()}
    assert set(rows) == {"A", "B", "C", "D"}
    assert {s: rows[s]["decision"] for s in rows} == {
        "A": "TAKEN",
        "B": "TAKEN",
        "C": "TAKEN",
        "D": "SKIPPED",
    }
    assert rows["D"]["reason"] == "SLOTS_FULL" and rows["D"]["entry"] is None
    a = rows["A"]
    assert (a["time"], a["side"], a["OR_H"], a["OR_L"]) == ("09:44", "long", 101.0, 100.0)
    assert a["entry"] == pytest.approx(EXPECTED["A"]["entry"])
    assert a["net_pnl"] == pytest.approx(EXPECTED["A"]["net"], abs=0.01)
    assert a["exit_reason"] == "STOP" and a["score"] == pytest.approx(100.0)


def test_labels_for_every_breakout_including_skipped(res):
    lb = lab.labels(res.signals, res.sims)
    assert lb.height == 4 * 2  # 4 breakouts x 2 variants
    d = lb.filter((pl.col("symbol") == "D") & (pl.col("variant") == "default")).row(0, named=True)
    # D was skipped (slots full) but is labelled as if taken
    assert d["decision"] == "QUALIFIED" and d["sim_status"] == "OK" and d["qty"] >= 1
    assert d["net_pnl"] is not None and d["exit_reason"] == "HARD_EXIT"
    a = lb.filter((pl.col("symbol") == "A") & (pl.col("variant") == "default")).row(0, named=True)
    assert a["r_net"] == pytest.approx(EXPECTED["A"]["net"] / (26 * 1.44), abs=1e-6)
    assert a["mae_r"] > 1.0  # stopped out beyond 1R after slippage
    assert a["r_per_share_gross"] == pytest.approx((99.97 - 101.44) / 1.44, abs=1e-6)


def test_feature_snapshot_reproduces_score(res, cfg):
    snap = lab.feature_snapshot(res.signals, "rule_v1", cfg.hash())
    scorer = RuleScorer(cfg.scoring)
    for r in snap.to_dicts():
        feats = {
            "side": r["side_sign"],
            "d": r["d"],
            "rv": r["rv"],
            "r_idx": r["r_idx"],
            "w": r["w"],
            "v": r["v"],
        }
        assert scorer.score(feats) == pytest.approx(r["score"])
    assert set(snap["config_hash"]) == {cfg.hash()}


def test_run_folder_contains_deliverable5_files(res, cfg, tmp_path):
    out = write_run(res, cfg, tmp_path, {"scorer": "rule_v1"})
    assert {"signal_log.csv", "features.parquet", "labels.parquet"} <= {
        p.name for p in out.iterdir()
    }
    assert pl.read_csv(out / "signal_log.csv").height == 4
