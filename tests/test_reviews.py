from datetime import date

import polars as pl
import pytest

from orb.reviews import pending_reviews, require_reviewed


def _manual(tmp_path, files: dict[str, str]):
    d = tmp_path / "manual"
    d.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (d / name).write_text(body)
    return tmp_path


def test_pending_rows_found_in_every_manual_file(tmp_path):
    root = _manual(
        tmp_path,
        {
            "mergers.csv": (
                "old_symbol,new_symbol,effective_date,needs_review\n"
                "HDFC,HDFCBANK,2023-07-13,false\n"
            ),
            "calendar_exceptions_manual.csv": (
                "date,reason,needs_review\n2021-02-24,outage,true\n2017-07-10,outage,false\n"
            ),
            "nifty200_changes_manual.csv": (
                "effective_date,symbol,change,needs_review\n"
                "2021-09-30,ASTRAL,add,TRUE\n2021-09-30,BBTC,remove,\n"
            ),
            # no needs_review column: ignored
            "special_session_candidates.csv": "date,label\n2024-11-01,Muhurat\n",
        },
    )
    p = pending_reviews(root)
    assert p == {
        "calendar_exceptions_manual.csv": ["2021-02-24"],
        "nifty200_changes_manual.csv": ["2021-09-30 ASTRAL", "2021-09-30 BBTC"],
    }
    with pytest.raises(SystemExit, match="backtest refused"):
        require_reviewed(root)


def test_all_reviewed_passes(tmp_path):
    root = _manual(tmp_path, {"mergers.csv": "old_symbol,needs_review\nA,false\nB,0\n"})
    assert pending_reviews(root) == {}
    require_reviewed(root)  # no exception


def test_manual_files_build_reference_files_from_reviewed_rows(tmp_path, cfg):
    from orb.refdata.build import RefBuilder

    root = _manual(
        tmp_path,
        {
            "calendar_exceptions_manual.csv": "date,reason,source,needs_review,note\n"
            "2017-07-10,outage,x,false,\n2021-02-24,outage,x,true,\n",
            "budget_days_manual.csv": "date,source,needs_review,note\n2020-02-01,x,false,\n",
        },
    )
    c = cfg.model_copy(update={"reference": cfg.reference.model_copy(update={"root": str(root)})})
    b = RefBuilder(c, fetcher=object())
    b._from_manual(
        "calendar_exceptions_manual.csv", ["date", "reason"], c.reference.calendar_exceptions
    )
    b._from_manual("budget_days_manual.csv", ["date"], c.reference.budget_days)
    ce = pl.read_csv(root / c.reference.calendar_exceptions)
    assert ce.rows() == [("2017-07-10", "outage")]  # the pending 2021-02-24 is not applied
    assert pl.read_csv(root / c.reference.budget_days)["date"].to_list() == ["2020-02-01"]


def test_budget_day_regime_table():
    from orb import reports

    T = date(2025, 2, 1)
    trades = pl.DataFrame(
        {
            "date": [T, date(2025, 2, 3)],
            "symbol": ["A", "B"],
            "net_pnl": [5.0, -2.0],
            "gross_pnl": [6.0, -1.0],
            "costs": [1.0, 1.0],
            "net_pnl_rounded": [5.0, -2.0],
            "slippage": [0.1, 0.1],
            "hold_minutes": [100, 50],
            "r_gross": [0.2, -0.1],
            "r_net": [0.18, -0.12],
        }
    )
    tags = reports.RegimeTags(
        vix=pl.DataFrame(schema={"date": pl.Date, "vix_tercile": pl.String}),
        trend=pl.DataFrame(schema={"date": pl.Date, "trend_day": pl.Boolean}),
        expiry=pl.DataFrame(schema={"date": pl.Date, "is_expiry": pl.Boolean}),
        results=pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date}),
        budget=pl.DataFrame({"date": [T]}),
    )
    t = {
        r["budget_day"]: r["trades"]
        for r in reports.regime_tables(trades, tags)["budget_day"].to_dicts()
    }
    assert t == {True: 1, False: 1}
