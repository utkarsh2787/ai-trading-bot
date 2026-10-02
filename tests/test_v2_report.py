"""V2 deliverable 6: report layout, criteria, nulls, per-strategy provenance, CLI."""

from datetime import date

import numpy as np
import polars as pl
import pytest

from orb import provenance as pv
from orb.reports import RegimeTags
from orb.v2 import report as rp
from orb.v2.engine import EngineV2, write_run_v2
from tests.test_provenance import commit, make_repo
from tests.test_v2_golden import golden_market
from tests.v2_market import T, builder


def empty_tags():
    return RegimeTags(
        vix=pl.DataFrame(schema={"date": pl.Date, "vix_tercile": pl.String}),
        trend=pl.DataFrame(schema={"date": pl.Date, "trend_day": pl.Boolean}),
        expiry=pl.DataFrame(schema={"date": pl.Date, "is_expiry": pl.Boolean}),
        results=pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date}),
    )


@pytest.fixture(scope="module")
def golden_run(cfg_v2, tmp_path_factory):
    m = golden_market(cfg_v2)
    res = EngineV2(cfg_v2, builder(m), {(s, T): 0.01 for s in m.symbols}, None, {}).run(T, T)
    out = write_run_v2(res, cfg_v2, tmp_path_factory.mktemp("runs"), {"config_hash": cfg_v2.hash()})
    return out


def test_report_layout(golden_run, cfg_v2):
    lines = (rp.build_report_v2(golden_run, cfg_v2, empty_tags()) / "report.md").read_text()
    ls = lines.splitlines()
    assert ls[0].startswith("survivorship gap:")
    assert [x[:9] for x in ls[1:6]] == ["[FAIL] a.", "[PASS] b.", ls[3][:9], "[PASS] d.", ls[5][:9]]
    assert ls[1].endswith(": 3")
    assert "p < 0.025 (Bonferroni, 2 strategies)" in ls[3]
    assert ls[6] == "IN-SAMPLE GATE (V2, a-d): FAIL -> OOS run NOT allowed"
    assert ls[7].startswith("capital model: equity tracked per book")
    assert ls[8].startswith("ruin dates: primary never")
    order = [
        "## Headline metrics",
        "## Skips and flags",
        "### Primary: random direction",
        "### Secondary: always-long",
        "### Secondary: index-level",
        "## Secondary diagnostics",
        "## Criterion e",
    ]
    pos = [lines.index(s) for s in order]
    assert pos == sorted(pos)
    head = pl.read_csv(golden_run / "report" / "summary.csv")
    p = head.filter(pl.col("book") == "primary").row(0, named=True)
    assert p["trades"] == 3 and p["net_pnl_rounded"] == pytest.approx(10.3394, abs=1e-3)
    assert p["final_equity"] == pytest.approx(10010.34, abs=1e-2) and p["exit_substituted"] == 1


def test_null_flips_only_the_raw_move():
    t = pl.DataFrame(
        {
            "raw_move": [20.8, -16.9],
            "slippage_paid": [0.52, 0.52],
            "costs": [2.8, 2.79],
            "net_pnl": [17.48, -20.21],
            "side": ["long", "long"],
        }
    )
    out = rp.null_primary(t, 2000, 1)
    # actual = raw - slip - costs; twin = -raw - slip - costs; both totals use unrounded costs
    assert out["actual_total"] == pytest.approx(20.8 - 16.9 - 0.52 * 2 - 2.8 - 2.79, abs=0.01)
    a = np.array([20.8 - 3.32, -16.9 - 3.31])
    b = np.array([-20.8 - 3.32, 16.9 - 3.31])
    assert out["mean_diff_per_trade"] == pytest.approx(float((a - (a + b) / 2).mean()), abs=1e-4)


def test_always_long():
    t = pl.DataFrame(
        {
            "raw_move": [10.0, 5.0],
            "slippage_paid": [1.0, 1.0],
            "costs": [2.0, 2.0],
            "net_pnl": [7.0, 2.0],
            "side": ["long", "short"],
        }
    )
    al = rp.always_long(t)
    # long leg as is: 10 - 3 = 7; the short's raw move flips for a long: -5 - 3 = -8
    assert al["net_always_long_unrounded"] == pytest.approx(-1.0)


def _book(nets, book="primary", years=None, stress=None):
    rows = []
    for i, n in enumerate(nets):
        d = date(2018 + (years[i] if years else 0), 1, 2 + i % 20)
        for b, v in ((book, n), ("v1_model", stress[i] if stress else n)):
            rows.append(
                {
                    "date": d,
                    "symbol": f"S{i}",
                    "book": b,
                    "book_decision": "TAKEN",
                    "net_pnl": v,
                    "net_pnl_rounded": v,
                }
            )
    return pl.DataFrame(rows)


def test_criteria_pass_and_each_fails(cfg_v2):
    labels = pl.DataFrame(
        {
            "status": ["OK"] * 6,
            "abs_z": [0.3, 0.4, 0.6, 0.7, 1.0, 1.2],
            "ret_net": [0.001, 0.001, 0.002, 0.002, 0.003, 0.003],
            "ret_gross": [0.0] * 6,
            "net_pnl": [1.0] * 6,
        }
    )
    good = _book([5.0] * 300, years=[i % 5 for i in range(300)])
    meta = {"ruin": {"primary": None}}
    crit, verdict = rp.criteria(good, {"p_value": 0.01}, labels, meta, cfg_v2, oos=False)
    assert all(c.passed for c in crit) and verdict.endswith("OOS run allowed")
    bad_c = rp.criteria(good, {"p_value": 0.03}, labels, meta, cfg_v2, False)  # 0.03 >= 0.025
    assert not bad_c[0][2].passed and "NOT allowed" in bad_c[1]
    ruined = rp.criteria(
        good, {"p_value": 0.01}, labels, {"ruin": {"primary": "2020-03-23"}}, cfg_v2, False
    )
    assert not ruined[0][3].passed and "ruin 2020-03-23" in ruined[0][3].value
    stress_neg = _book([5.0] * 300, years=[i % 5 for i in range(300)], stress=[-1.0] * 300)
    assert not rp.criteria(stress_neg, {"p_value": 0.01}, labels, meta, cfg_v2, False)[0][1].passed
    few = _book([5.0] * 10)
    assert not rp.criteria(few, {"p_value": 0.01}, labels, meta, cfg_v2, False)[0][0].passed
    flat_e = labels.with_columns(ret_net=pl.lit(0.001))
    assert not rp.criteria(good, {"p_value": 0.01}, flat_e, meta, cfg_v2, False)[0][4].passed


# --------------------------------------------------------- provenance per strategy


def test_changelog_sections_are_scoped():
    text = "# Research changelog\n\n# V1\n\n- Commit: `aaaaaaa`\n\n# V2\n\n- Commit: `bbbbbbb`\n"
    assert "aaaaaaa" in pv.changelog_section(text, "V1")
    assert "bbbbbbb" not in pv.changelog_section(text, "V1")
    assert "bbbbbbb" in pv.changelog_section(text, "V2")
    assert "aaaaaaa" not in pv.changelog_section(text, "V2")
    old = "# Research changelog\n\n## 2026-10-10\n- Commit: `ccccccc`\n"  # pre-V2 layout
    assert "ccccccc" in pv.changelog_section(old, "V1")
    assert pv.changelog_section(old, "V2") == ""


def test_v2_pin_and_gate_are_separate(tmp_path):
    repo = make_repo(tmp_path / "r")
    data = tmp_path / "data"
    v1 = pv.save_pin(data, pv.preflight(repo, data, False)["commit"], "v1run")
    p2 = pv.preflight(repo, data, False, strategy="v2")
    assert p2["will_pin"]  # V1's pin doesn't count for V2
    pv.save_pin(data, p2["commit"], "v2run", strategy="v2")
    assert pv.load_pin(data, "v2")["run_id"] == "v2run" and pv.load_pin(data)["run_id"] == "v1run"
    assert pv.pin_path(data, "v2") == data / "_runs" / "v2" / "insample_pin.json"
    fix = commit(repo, {"src/orb/v2/trade.py": "fix\n"}, "v2 fix")
    with pytest.raises(SystemExit, match=r"(?s)not logged under '# V2'.*v2 fix"):
        pv.preflight(repo, data, False, strategy="v2")
    assert pv.preflight(repo, data, False)["pinned_commit"] == v1["commit"]  # V1 unaffected
    # logged under V1 only -> still refused for V2
    commit(repo, {"docs/CHANGELOG_RESEARCH.md": f"# V1\n- `{fix[:12]}`\n\n# V2\n"}, "log")
    with pytest.raises(SystemExit, match="not logged under '# V2'"):
        pv.preflight(repo, data, False, strategy="v2")
    commit(repo, {"docs/CHANGELOG_RESEARCH.md": f"# V1\n\n# V2\n- `{fix[:12]}`\n"}, "log v2")
    assert pv.preflight(repo, data, False, strategy="v2")["diff_since_pin"]["relevant_files"] == [
        "src/orb/v2/trade.py"
    ]


def test_cli_strategy_v2_loads_v2_config(monkeypatch):
    from orb import cli
    from orb.v2 import run as v2run
    from orb.v2.config import ConfigV2

    seen = {}
    monkeypatch.setattr(v2run, "cmd_report_v2", lambda cfg, args: seen.update(cfg=cfg, args=args))
    cli.main(["report", "runs/v2/x", "--strategy", "v2"])
    assert isinstance(seen["cfg"], ConfigV2) and seen["args"].run_dir == "runs/v2/x"


def test_v2_oos_locked(cfg_v2, tmp_path):
    from orb.engine import OOSLockError

    m = golden_market(cfg_v2)
    e = EngineV2(cfg_v2, builder(m), {}, None, {})
    with pytest.raises(OOSLockError):
        e.run(date(2024, 9, 30), date(2024, 10, 1), ledger=tmp_path / "l.jsonl")
