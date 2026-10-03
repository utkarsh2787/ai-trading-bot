"""V3 deliverable 6: engine (books, labels, null, baselines), report, gate tooling, CLI."""

from datetime import date

import polars as pl
import pytest

from orb import provenance as pv
from orb.v3 import report as rp
from orb.v3.engine import EngineV3, write_run_v3
from tests.test_provenance import commit, make_repo
from tests.test_v2_report import empty_tags
from tests.v3_market import CAL, day_bars, make_data

START, END = date(2024, 5, 6), date(2024, 6, 28)
SYMS = list("ABCDEFGH")


def price(s: str, d: date) -> float:
    k = (ord(s) * 7 + d.toordinal() * 3) % 11
    return round(100.0 + k - 5 + (d.toordinal() % 5) * 0.3, 2)


@pytest.fixture(scope="module")
def engine_run(cfg_v3, tmp_path_factory):
    days = [d for d in CAL if d >= date(2024, 4, 26)]
    closes = {(s, d): price(s, d) for s in SYMS for d in CAL}
    minute = {(s, d): day_bars(price(s, d)) for s in SYMS for d in days}
    data = make_data(
        cfg_v3,
        SYMS,
        minute,
        closes,
        actions=[("C", date(2024, 5, 22), "dividend", None, "Rs 1.5 Per Share")],
    )
    idx = pl.DataFrame({"date": CAL, "close": [20000.0 + i * 10 for i in range(len(CAL))]})
    eng = EngineV3(cfg_v3, data, idx)
    tmp = tmp_path_factory.mktemp("v3")
    res = eng.run(START, END, ledger=tmp / "l.jsonl", null_draws=40)
    out = write_run_v3(res, cfg_v3, tmp / "runs", {"config_hash": cfg_v3.hash()})
    return res, out


def test_engine_outputs(engine_run, cfg_v3):
    res, _ = engine_run
    assert set(res.books) == {"primary", "primary_2x", "v1_model"}
    t = res.trades.filter(pl.col("book") == "primary")
    assert t.height > 0 and set(t["exit_cause"]) <= {"REBALANCE", "FORCED_END"}
    assert res.books["primary"]["final_equity"] == pytest.approx(
        10_000 + t["net_pnl_rounded"].sum(), abs=1e-6
    )
    # every target set is the two lowest r_week stocks of that week
    for _, g in res.signals.group_by("date"):
        e = g.filter(pl.col("eligible")).sort(["r_week", "symbol"])
        assert set(g.filter(pl.col("target"))["symbol"]) == set(e["symbol"].head(2))
    assert len(res.null["final_equity"]) == 40
    assert res.labels.height > 0 and res.labels["ret_gross"].null_count() < res.labels.height
    assert res.baselines["index_buy_hold"]["final_equity"] > 10_000
    assert set(res.baselines["equal_weight"]) == {"pct_costs", "with_dp"}
    ew = res.baselines["equal_weight"]
    assert ew["with_dp"]["final_equity"] < ew["pct_costs"]["final_equity"]


def test_labels_match_prices(engine_run):
    res, _ = engine_run
    lb = res.labels.filter((pl.col("symbol") == "A") & pl.col("ret_gross").is_not_null()).row(
        0, named=True
    )
    assert lb["ret_gross"] == pytest.approx(price("A", lb["next"]) / price("A", lb["date"]) - 1)
    c = res.labels.filter((pl.col("symbol") == "C") & (pl.col("date") == date(2024, 5, 17))).row(
        0, named=True
    )
    assert c["ret_gross"] == pytest.approx(
        (price("C", date(2024, 5, 24)) + 1.5) / price("C", date(2024, 5, 17)) - 1
    )


def test_null_draws_reproducible(cfg_v3, engine_run):
    res, _ = engine_run
    days = [d for d in CAL if d >= date(2024, 4, 26)]
    closes = {(s, d): price(s, d) for s in SYMS for d in CAL}
    minute = {(s, d): day_bars(price(s, d)) for s in SYMS for d in days}
    eng = EngineV3(
        cfg_v3,
        make_data(
            cfg_v3,
            SYMS,
            minute,
            closes,
            actions=[("C", date(2024, 5, 22), "dividend", None, "Rs 1.5 Per Share")],
        ),
    )
    eng.market.precompute(eng.market.rebalances(START, END), END)
    again = eng.null(START, END, 40)
    assert again["final_equity"] == res.null["final_equity"]


def test_report_layout(engine_run, cfg_v3):
    _, out = engine_run
    text = (rp.build_report_v3(out, cfg_v3, empty_tags()) / "report.md").read_text()
    ls = text.splitlines()
    assert ls[0].startswith("survivorship gap:") and ls[0].endswith("[rebalance days]")
    assert [x[7:9] for x in ls[1:6]] == ["a.", "b.", "c.", "d.", "e."]
    assert "p < 0.0167" in ls[3] and "Bonferroni, 3 strategies" in ls[3]
    assert ls[6].startswith("IN-SAMPLE GATE (V3, a-d): FAIL")  # far fewer than 300 round trips
    assert ls[7].startswith("capital model: cash ledger per book")
    assert ls[8].startswith("ruin dates: primary never")
    assert "PRE-TAX" in ls[9] and "beta alone" in ls[9]
    order = [
        "## Metrics per book",
        "### Primary: random selection",
        "### Annual net vs the null",
        "### Secondary baselines",
        "## Secondary diagnostics",
        "## Flag counts",
        "## Criterion e",
    ]
    pos = [text.index(s) for s in order]
    assert pos == sorted(pos)
    sd = pl.read_csv(out / "report" / "secondary_diagnostics.csv")
    assert sd["scenario"].to_list()[:3] == ["all trades (primary)", "DP x 0", "DP x 2"]
    all_, dp0, dp2 = sd["net_pnl_rounded"].to_list()[:3]
    n = sd["round_trips"][0]
    assert dp0 == pytest.approx(all_ + 15.34 * n, abs=0.02) and dp2 == pytest.approx(
        all_ - 15.34 * n, abs=0.02
    )


def test_per_year_and_null_summary():
    py = rp.per_year(
        {"2018": 10_500.0, "2019": 10_300.0},
        [{2018: 10_100.0, 2019: 10_400.0}, {2018: 10_300.0, 2019: 10_100.0}],
        10_000,
    )
    assert py["actual_net"].to_list() == [500.0, -200.0]
    assert py["null_mean_net"].to_list() == [200.0, 50.0]
    assert py["beats_null"].to_list() == [True, False]
    ns = rp.null_summary(105.0, [100.0, 110.0, 105.0, 90.0])
    assert ns["p_value"] == 0.5  # 110 and 105 are >= 105


def test_quintiles_top_first():
    rows = [
        {
            "date": date(2024, 1, 5),
            "symbol": f"S{i}",
            "r_week": i / 100,
            "ret_gross": -i / 1000,
            "ret_net": -i / 1000 - 0.001,
        }
        for i in range(10)
    ]
    q = rp.quintiles(pl.DataFrame(rows), 5)
    assert q["quintile"].to_list() == [5, 4, 3, 2, 1]
    m = q["avg_ret_gross"].to_list()
    assert all(b > a for a, b in zip(m, m[1:], strict=False))  # reversal: rises top -> bottom


def test_criteria_pass_and_fail(cfg_v3):
    t = pl.DataFrame(
        {"book": ["primary"] * 300 + ["v1_model"] * 300, "net_pnl_rounded": [1.0] * 600}
    )
    ye = {2018 + i: 10_000 + 100 * (i + 1) for i in range(5)}
    meta = {"books": {"primary": {"final_equity": 10_500.0, "year_end": ye, "ruin_date": None}}}
    null_ye = [{y: 10_000 + 50 * (y - 2017) for y in ye}] * 3
    labels = pl.DataFrame(
        {
            "date": [date(2024, 1, 5)] * 10,
            "r_week": [i / 100 for i in range(10)],
            "ret_gross": [-i / 1000 for i in range(10)],
            "ret_net": [0.0] * 10,
        }
    )
    crit, verdict, *_ = rp.criteria(meta, t, [10_000.0] * 2000, null_ye, labels, cfg_v3, False)
    assert all(c.passed for c in crit) and verdict.endswith("OOS run allowed")
    crit, verdict, *_ = rp.criteria(
        meta, t, [10_000.0] * 1960 + [11_000.0] * 40, null_ye, labels, cfg_v3, False
    )
    assert not crit[2].passed  # p = 0.02 >= 0.0167
    ruined = {"books": {"primary": {**meta["books"]["primary"], "ruin_date": "2020-03-20"}}}
    assert not rp.criteria(ruined, t, [10_000.0] * 2000, null_ye, labels, cfg_v3, False)[0][
        3
    ].passed


def test_v3_pin_and_changelog_section(tmp_path):
    repo = make_repo(tmp_path / "r")
    data = tmp_path / "data"
    p = pv.preflight(repo, data, False, strategy="v3")
    assert p["will_pin"]
    pv.save_pin(data, p["commit"], "v3run", strategy="v3")
    assert pv.pin_path(data, "v3") == data / "_runs" / "v3" / "insample_pin.json"
    fix = commit(repo, {"src/orb/v3/ledger.py": "fix\n"}, "v3 fix")
    with pytest.raises(SystemExit, match="not logged under '# V3'"):
        pv.preflight(repo, data, False, strategy="v3")
    commit(repo, {"docs/CHANGELOG_RESEARCH.md": f"# V2\n- `{fix[:12]}`\n\n# V3\n"}, "log in V2")
    with pytest.raises(SystemExit, match="not logged under '# V3'"):
        pv.preflight(repo, data, False, strategy="v3")
    commit(repo, {"docs/CHANGELOG_RESEARCH.md": f"# V2\n\n# V3\n- `{fix[:12]}`\n"}, "log in V3")
    assert pv.preflight(repo, data, False, strategy="v3")["pinned_commit"]


def test_cli_strategy_v3(monkeypatch):
    from orb import cli
    from orb.v3 import run as v3run
    from orb.v3.config import ConfigV3

    seen = {}
    monkeypatch.setattr(v3run, "cmd_report_v3", lambda cfg, args: seen.update(cfg=cfg))
    cli.main(["report", "runs/v3/x", "--strategy", "v3"])
    assert isinstance(seen["cfg"], ConfigV3)


def test_v3_oos_locked(cfg_v3, tmp_path):
    from orb.engine import OOSLockError

    eng = EngineV3(cfg_v3, make_data(cfg_v3, ["A"], {}))
    with pytest.raises(OOSLockError):
        eng.run(date(2024, 9, 1), date(2024, 10, 4), ledger=tmp_path / "l.jsonl", null_draws=1)
