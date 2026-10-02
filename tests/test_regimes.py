from datetime import date

import polars as pl

from orb.regimes import expiry_flags, trend_days


def test_expiry_flags():
    e = pl.DataFrame(
        {
            "date": [date(2024, 1, 4), date(2024, 1, 25), date(2024, 1, 25)],
            "expiry_type": ["index_weekly", "index_monthly", "stock_monthly"],
        }
    )
    f = {r["date"]: r for r in expiry_flags(e).to_dicts()}
    assert f[date(2024, 1, 4)]["is_index_weekly"] and not f[date(2024, 1, 4)]["is_stock_monthly"]
    assert f[date(2024, 1, 25)]["is_stock_monthly"] and f[date(2024, 1, 25)]["is_expiry"]


def test_trend_days():
    d = pl.DataFrame(
        {
            "date": [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)],
            "open": [100.0, 100.0, 100.0],
            "high": [110.0, 110.0, 100.0],
            "low": [99.0, 90.0, 100.0],
            "close": [108.0, 101.0, 100.0],
        }
    )
    assert trend_days(d, 0.5)["trend_day"].to_list() == [True, False, False]


def _vix(values):
    days = [date(2020, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(len(values))]
    return pl.DataFrame({"date": days, "close": [float(x) for x in values]})


def test_vix_terciles_expanding_hand_checked():
    from orb.regimes import vix_terciles

    t = vix_terciles(_vix([10, 20, 30, 40, 5]), min_history=3).to_dicts()
    assert t[0]["vix_prev_close"] is None and t[2]["vix_tercile"] is None  # < 3 closes
    # day 3: prev close 30, history [10, 20, 30] -> q1 = 16.67, q2 = 23.33 -> high
    assert t[3]["vix_prev_close"] == 30.0 and t[3]["vix_tercile"] == "high"
    assert abs(t[3]["q1"] - 50 / 3) < 1e-9
    # day 4: prev close 40, history [10, 20, 30, 40] -> q2 = 30 -> high
    assert t[4]["vix_tercile"] == "high" and t[4]["q2"] == 30.0


def test_vix_terciles_never_use_future_data():
    from orb.regimes import vix_terciles

    base = [12, 15, 11, 18, 22, 14, 13, 30, 25, 16, 19, 21]
    a = vix_terciles(_vix(base), min_history=4)
    b = vix_terciles(_vix(base[:8] + [99, 1, 50, 2]), min_history=4)  # change the future
    cols = ["vix_prev_close", "q1", "q2", "vix_tercile"]
    assert a.head(9).select(cols).equals(b.head(9).select(cols))  # day 8 uses closes <= day 7
    assert not a.select(cols).equals(b.select(cols))
