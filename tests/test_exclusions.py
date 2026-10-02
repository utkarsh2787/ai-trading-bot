from datetime import date

import polars as pl
import pytest

from orb.data.exclusions import exclusion_report, exclusion_table, universe_days
from orb.data.reference import ReferenceData, load_table
from orb.regimes import vix_terciles

D1, D2, D3 = date(2024, 1, 1), date(2024, 1, 2), date(2025, 1, 1)


def _ref(tmp_path):
    def t(name):
        return load_table(name, tmp_path / "none.csv", required=False)

    membership = pl.DataFrame(
        {"symbol": ["A", "B"], "valid_from": [D1, D1], "valid_to": [None, D2]},
        schema_overrides={"valid_to": pl.Date},
    )
    ban = pl.DataFrame({"date": [D1], "symbol": ["B"]})
    special = pl.DataFrame({"date": [D3], "session_type": ["muhurat"]})
    return ReferenceData(
        membership=membership,
        ban_list=ban,
        corporate_actions=t("corporate_actions"),
        special_sessions=special,
        calendar_exceptions=t("calendar_exceptions"),
        budget_days=t("budget_days"),
        symbol_map=t("symbol_map"),
        expiries=t("expiries"),
        results_dates=t("results_dates"),
    )


def test_exclusion_table_and_report(tmp_path):
    ref = _ref(tmp_path)
    uni = universe_days(ref.membership, [D1, D2, D3])
    assert uni.height == 5  # A x3, B x2 (B left after D2)
    issues = pl.DataFrame(
        {
            "symbol": ["A", "A", "Z"],
            "date": [D2, D2, D2],
            "check": ["duplicate_timestamp", "volume_spike", "extreme_gap"],
            "severity": ["error", "warn", "error"],
            "value": [1.0, 1, 1],
            "detail": ["", "", ""],
        }
    )
    excl = exclusion_table(issues, ref, ["split"], uni)
    got = {(r["symbol"], r["date"], r["reason"]) for r in excl.to_dicts()}
    assert got == {
        ("A", D2, "DUPLICATE_TIMESTAMP"),  # dq error (warn ignored)
        ("B", D1, "FNO_BAN"),  # reference
        ("A", D3, "SPECIAL_SESSION_MUHURAT"),
    }  # calendar, members only
    # Z is not a member -> not counted
    vix = pl.DataFrame({"date": [D1, D2, D3], "vix_tercile": ["low", "high", "high"]})
    rep = exclusion_report(excl, uni, vix)
    allrow = rep["by_reason"].filter(pl.col("reason") == "ALL").row(0, named=True)
    assert allrow["stock_days"] == 3 and allrow["pct"] == pytest.approx(60.0)
    y = rep["by_year"].filter((pl.col("year") == 2024) & (pl.col("reason") == "ALL"))
    assert y["stock_days"][0] == 2 and y["universe_days"][0] == 4
    hv = rep["by_vix_tercile"].filter(
        (pl.col("vix_tercile") == "high") & (pl.col("reason") == "ALL")
    )
    assert hv["stock_days"][0] == 2 and hv["universe_days"][0] == 3


def test_vix_terciles_use_prev_close_and_in_sample_cuts():
    days = [date(2020, 1, d) for d in range(1, 11)]
    vix = pl.DataFrame({"date": days, "close": [float(x) for x in range(10, 20)]})
    t = vix_terciles(vix, date(2020, 1, 1), date(2020, 1, 7))
    assert t["vix_prev_close"][0] is None and t["vix_tercile"][0] is None
    assert t["vix_prev_close"][1] == 10.0
    # in-sample prev closes 10..15 -> cuts at 11.67 / 13.33; OOS 16..18 -> high
    assert t.filter(pl.col("date") >= date(2020, 1, 8))["vix_tercile"].to_list() == ["high"] * 3
    assert t["vix_tercile"][1] == "low"
