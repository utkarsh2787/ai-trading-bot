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
