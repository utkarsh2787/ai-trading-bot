"""Deliverable 6 on the hand-computed golden day (constants from test_engine_golden)."""

import json
from datetime import date

import numpy as np
import polars as pl
import pytest

from orb import reports
from orb.engine import Engine, write_run
from orb.scoring import RuleScorer
from orb.sim.ticks import daily_ticks
from tests.test_engine_golden import DAY_NET, DAY_NET_ROUNDED, EXPECTED, golden_day

T = date(2024, 6, 28)


@pytest.fixture(scope="module")
def run(cfg, tmp_path_factory):
    m, day = golden_day(cfg)
    ticks = {
        (r["symbol"], r["date"]): r["tick"] for r in daily_ticks(m.daily, cfg.ticks).to_dicts()
    }
    res = Engine(cfg, m.builder(), ticks, RuleScorer(cfg.scoring)).run(
        day, day, ledger=tmp_path_factory.mktemp("l") / "l.jsonl"
    )
    out = write_run(res, cfg, tmp_path_factory.mktemp("runs"), {})
    return res, out


def test_trade_log_and_summary(run, cfg):
    res, _ = run
    tl = reports.trade_log(res.book, cfg)
    assert tl.height == 3 * 6  # 3 taken trades x (2 variants x 3 slippage)
    a = tl.filter(
        (pl.col("symbol") == "A")
        & (pl.col("variant") == "default")
        & (pl.col("slippage_mult") == 1.0)
    ).row(0, named=True)
    assert (a["entry_time"].strftime("%H:%M"), a["exit_time"].strftime("%H:%M")) == (
        "09:45",
        "12:35",
    )
    s = (
        reports.summary(tl)
        .filter((pl.col("variant") == "default") & (pl.col("slippage_mult") == 1.0))
        .row(0, named=True)
    )
    nets = [EXPECTED[k]["net"] for k in "ABC"]
    assert s["trades"] == 3 and s["hit_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert s["net_pnl"] == pytest.approx(DAY_NET, abs=0.01)
    assert s["net_pnl_rounded"] == pytest.approx(DAY_NET_ROUNDED, abs=0.01)
    assert s["gross_pnl"] == pytest.approx(-38.22 + 11.88 - 59.02, abs=0.01)
    assert s["avg_win"] == pytest.approx(EXPECTED["B"]["net"], abs=0.01)
    assert s["avg_loss"] == pytest.approx((nets[0] + nets[2]) / 2, abs=0.01)
    assert s["max_drawdown"] == pytest.approx(-DAY_NET, abs=0.01)
    assert (s["stops"], s["hard_exits"], s["exits_substituted"]) == (2, 1, 0)
    sub = tl.with_columns(
        exit_reason=pl.when(pl.col("symbol") == "B")
        .then(pl.lit("EXIT_SUBSTITUTED"))
        .otherwise("exit_reason")
    )
    s2 = (
        reports.summary(sub)
        .filter((pl.col("variant") == "default") & (pl.col("slippage_mult") == 1.0))
        .row(0, named=True)
    )
    assert (s2["hard_exits"], s2["exits_substituted"]) == (0, 1)
    assert s["profit_factor"] == pytest.approx(nets[1] / -(nets[0] + nets[2]), abs=1e-3)


def test_null_twins_and_bootstrap(run, cfg):
    res, _ = run
    null = res.null
    assert null.height == 3 and set(null["twin_status"]) == {"OK"}
    a = null.filter(pl.col("symbol") == "A").row(0, named=True)
    # twin of A: short at 09:45 open 101.40 -> 101.36972 -> 101.36; stop 101.36 + 1.44 = 102.80
    # never hit; 15:10 open 100.50 -> buy 100.5301 -> 100.54; gross 26 x 0.82 = 21.32
    assert a["twin_side"] == "short" and a["twin_exit_reason"] == "HARD_EXIT"
    assert 21.32 - 3.0 < a["twin_net_pnl"] < 21.32
    nt = reports.null_test(null, 2000, 7)
    assert nt["usable"] == 3 and nt["actual_total"] == pytest.approx(DAY_NET, abs=0.01)
    exp_mean = (null["net_pnl"].sum() + null["twin_net_pnl"].sum()) / 2
    assert nt["random_mean"] == pytest.approx(exp_mean, abs=3.0)
    assert 0.0 <= nt["p_value"] <= 1.0 and nt["mean_diff_ci95"][0] <= nt["mean_diff_ci95"][1]
    assert reports.null_test(null, 2000, 7) == nt  # deterministic seed


def test_regime_tables(run, cfg):
    res, _ = run
    tl = reports.trade_log(res.book, cfg).filter(
        (pl.col("variant") == "default") & (pl.col("slippage_mult") == 1.0)
    )
    tags = reports.RegimeTags(
        vix=pl.DataFrame({"date": [T], "vix_tercile": ["high"]}),
        trend=pl.DataFrame({"date": [T], "trend_day": [False]}),
        expiry=pl.DataFrame({"date": [T], "is_stock_monthly": [True], "is_expiry": [True]}),
        results=pl.DataFrame({"symbol": ["A"], "date": [T]}),
    )
    r = reports.regime_tables(tl, tags)
    assert r["vix_tercile"].row(0, named=True)["vix_tercile"] == "high"
    assert r["trend_vs_range"]["day_type"].to_list() == ["range"]
    rd = {x["results_day"]: x["trades"] for x in r["results_day"].to_dicts()}
    assert rd == {False: 2, True: 1}
    assert r["expiry"]["trades"].to_list() == [3]


def test_score_validity_buckets_and_quintiles(cfg):
    rng = np.random.default_rng(0)
    n = 50
    scores = np.linspace(40, 100, n)
    lb = pl.DataFrame(
        {
            "date": [T] * n,
            "symbol": [f"S{i}" for i in range(n)],
            "variant": "default",
            "decision": ["REJECTED" if s < 65 else "QUALIFIED" for s in scores],
            "score": scores,
            "net_pnl": rng.normal(0, 10, n),
            "r_net": rng.normal(0, 0.5, n),
            "r_per_share_gross": rng.normal(0, 0.5, n),
        }
    )
    feats = pl.DataFrame(
        {
            "date": [T] * n,
            "symbol": lb["symbol"],
            "d": rng.random(n),
            "rv": rng.random(n) * 3,
            "w": rng.random(n),
            "v": rng.random(n) / 10,
            "r_idx": rng.normal(0, 0.003, n),
            "side_sign": np.ones(n),
        }
    )
    sv = reports.score_validity(lb, feats, [65, 75, 85], 5)
    b = {r["score_bucket"]: r["signals"] for r in sv["score_buckets"].to_dicts()}
    assert set(b) == {"<65", "65-74", "75-84", "85+"} and sum(b.values()) == n
    assert b["<65"] == int((scores < 65).sum())  # rejected signals are included
    q = sv["factor_quintiles"]
    assert set(q["factor"]) == {"d", "rv", "r_idx_aligned", "w", "v"}
    assert q.filter(pl.col("factor") == "d")["signals"].sum() == n
    assert sorted(q.filter(pl.col("factor") == "d")["quintile"].to_list()) == [1, 2, 3, 4, 5]


def test_build_report_end_to_end(run, cfg):
    _, out = run
    tags = reports.RegimeTags(
        vix=pl.DataFrame(schema={"date": pl.Date, "vix_tercile": pl.String}),
        trend=pl.DataFrame({"date": [T], "trend_day": [True]}),
        expiry=pl.DataFrame(schema={"date": pl.Date, "is_expiry": pl.Boolean}),
        results=pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date}),
    )
    rep = reports.build_report(out, cfg, tags)
    md = (rep / "report.md").read_text()
    assert md.splitlines()[0].startswith("survivorship gap:")
    assert "in-sample" in md and "Random-direction null" in md
    for f in (
        "trade_log.csv",
        "signal_log.csv",
        "summary.csv",
        "null.json",
        "score_buckets.csv",
        "regime_vix_tercile.csv",
        "regime_trend_vs_range.csv",
    ):
        assert (rep / f).exists(), f
    assert json.loads((rep / "null.json").read_text())["usable"] == 3


def test_null_twin_flips_only_direction(run):
    """Same qty, same costs, same entry candle, stop mirrored at the same distance;
    the exit time matches whenever neither stop is hit (B: both hold to 15:10)."""
    res, _ = run
    for r in res.null.to_dicts():
        assert r["twin_side"] != r["side"] and r["twin_status"] == "OK"
        assert r["twin_qty"] == r["qty"]
        assert r["twin_costs"] == pytest.approx(r["costs"])
        assert r["twin_net_pnl"] == pytest.approx(r["twin_gross_pnl"] - r["costs"])
        assert r["twin_entry_slot"] == r["entry_slot"]
        assert r["twin_risk_per_share"] == pytest.approx(r["risk_per_share"])
        assert r["flip_net_pnl"] == pytest.approx(-r["gross_pnl"] - r["costs"])
    b = res.null.filter(pl.col("symbol") == "B").row(0, named=True)
    assert b["same_exit_time"] and b["twin_exit_slot"] == b["exit_slot"] == 355
    a = res.null.filter(pl.col("symbol") == "A").row(0, named=True)
    assert not a["same_exit_time"]  # A stopped at 12:35; its mirrored twin held to 15:10
    nt = reports.null_test(res.null, 500, 1)
    assert nt["same_exit_time_share"] == pytest.approx(1 / 3, abs=1e-4)
    assert nt["secondary_sign_flip"]["usable"] == 3


def test_report_header_lists_criteria_under_survivorship_gap(run, cfg):
    _, out = run
    tags = reports.RegimeTags(
        vix=pl.DataFrame(schema={"date": pl.Date, "vix_tercile": pl.String}),
        trend=pl.DataFrame(schema={"date": pl.Date, "trend_day": pl.Boolean}),
        expiry=pl.DataFrame(schema={"date": pl.Date, "is_expiry": pl.Boolean}),
        results=pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date}),
    )
    lines = (reports.build_report(out, cfg, tags) / "report.md").read_text().splitlines()
    assert lines[0].startswith("survivorship gap:")
    assert [ln[:9] for ln in lines[1:6]] == [
        "[FAIL] a.",
        "[FAIL] b.",
        lines[3][:9],
        "[FAIL] d.",
        "[FAIL] e.",
    ]
    assert lines[1].endswith(": 3")  # 3 taken trades < 300
    assert lines[6].startswith("IN-SAMPLE GATE (V1, a-d): FAIL -> OOS run NOT allowed")


def _synthetic(n_years=5, per_year=80, edge=5.0):
    rows, lab = [], []
    rng = np.random.default_rng(3)
    for y in range(n_years):
        for i in range(per_year):
            d = date(2018 + y, 1 + i % 12, 1 + i % 28)
            for mult, cost in ((1.0, 2.0), (2.0, 3.0)):
                g = rng.normal(edge, 20)
                rows.append(
                    {
                        "date": d,
                        "symbol": f"S{i}",
                        "variant": "default",
                        "slippage_mult": mult,
                        "net_pnl_rounded": g - cost,
                        "net_pnl": g - cost,
                    }
                )
    for s in np.linspace(65, 100, 300):
        lab.append(
            {
                "variant": "default",
                "decision": "QUALIFIED",
                "score": float(s),
                "r_net": 0.05 + 0.01 * (s - 65) / 10 + rng.normal(0, 0.001),
            }
        )
    return pl.DataFrame(rows), pl.DataFrame(lab)


def test_criteria_all_pass_and_each_can_fail(cfg):
    trades, labels = _synthetic()
    crit, verdict = reports.criteria(trades, {"p_value": 0.01}, labels, cfg, oos=False)
    assert all(c.passed for c in crit), [c.line() for c in crit]
    assert verdict == "IN-SAMPLE GATE (V1, a-d): PASS -> OOS run allowed"
    # c fails on the null
    crit, verdict = reports.criteria(trades, {"p_value": 0.2}, labels, cfg, oos=False)
    assert not crit[2].passed and "NOT allowed" in verdict
    # b fails when 2x slippage is net negative
    t2 = trades.with_columns(
        net_pnl_rounded=pl.when(pl.col("slippage_mult") == 2.0)
        .then(-10.0)
        .otherwise("net_pnl_rounded")
    )
    assert not reports.criteria(t2, {"p_value": 0.01}, labels, cfg, oos=False)[0][1].passed
    # d fails when 3 of 5 years lose
    t3 = trades.with_columns(
        net_pnl_rounded=pl.when(pl.col("date").dt.year() <= 2020)
        .then(-1.0)
        .otherwise("net_pnl_rounded")
    )
    assert not reports.criteria(t3, {"p_value": 0.01}, labels, cfg, oos=False)[0][3].passed
    # e fails (score not useful) without blocking the a-d gate
    flat = labels.with_columns(r_net=pl.lit(0.05))
    crit, verdict = reports.criteria(trades, {"p_value": 0.01}, flat, cfg, oos=False)
    assert not crit[4].passed and "PASS -> OOS run allowed" in verdict
    assert "plain ORB" in verdict
    # OOS: criterion a is not part of the verdict
    few = trades.filter(pl.col("date").dt.year() == 2018).head(40)
    crit, verdict = reports.criteria(few, {"p_value": 0.01}, labels, cfg, oos=True)
    assert verdict.startswith("OOS VERDICT (V1): ")


def test_secondary_sign_flip_never_affects_pass_fail(cfg):
    trades, labels = _synthetic()
    good = {"p_value": 0.01, "secondary_sign_flip": {"p_value": 0.99}}
    bad = {"p_value": 0.20, "secondary_sign_flip": {"p_value": 0.0}}
    assert reports.criteria(trades, good, labels, cfg, oos=False)[0][2].passed
    assert not reports.criteria(trades, bad, labels, cfg, oos=False)[0][2].passed


def test_report_labels_secondary_null_as_excluded(run, cfg):
    _, out = run
    tags = reports.RegimeTags(
        vix=pl.DataFrame(schema={"date": pl.Date, "vix_tercile": pl.String}),
        trend=pl.DataFrame(schema={"date": pl.Date, "trend_day": pl.Boolean}),
        expiry=pl.DataFrame(schema={"date": pl.Date, "is_expiry": pl.Boolean}),
        results=pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date}),
    )
    md = (reports.build_report(out, cfg, tags) / "report.md").read_text()
    assert "Secondary diagnostic: strict sign flip" in md
    assert "Excluded from pass/fail" in md
